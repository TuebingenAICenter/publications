"""``pubstore-publish`` — one branch + one PR per publication, against the store repo.

Turns a Zotero RDF export into the store's go-forward ingestion shape: every item
becomes its own reviewable PR carrying the full canonical pair
(``entries/<year>/<key>.bib`` + ``meta/<year>/<key>.json``), instead of the single
bulk PR that landed the backfill. The same invocation runs on the scraper's monthly
cron and locally for staging.

Mechanics (decision 5 in the task plan):

- **No working tree.** PyGithub is API-only; the pair is committed via the GitHub
  Git Data API (build a tree off ``main`` → one commit → create ref → open PR). The
  bytes come from :func:`publication_store.emit.emit_pair`, so they are byte-identical
  to what CI ``pubstore-normalize`` would write (normalize stays a no-op).
- **Add / update / rename / skip.** Per item, against a one-shot recursive listing of
  ``entries/`` (store-wide, across *any* year shard, since citekeys are store-unique):
  a citekey not in the store is an **add** on branch ``<citekey>``; one in the store
  whose emitted pair differs byte-for-byte is an **update** on ``update/<citekey>``; an
  item carrying a ``Replaces: <old>`` *extra* line whose target is in the store is a
  **rename** on ``rename/<citekey>`` (writes the new pair, deletes ``<old>``'s in the
  same commit); a byte-identical pair, or an already-existing op branch, is **skipped**.
  Citekeys are ``[a-z0-9_-]`` ⇒ valid branch names. Because the citekey *is* the
  identity, a metadata edit that recomputes an unpinned key would orphan the old entry —
  so the run also **warns** on auto-generated (unpinned) keys.
- **Duplicate hints (render-only).** This CLI never computes similarity (``publib`` stays
  out of its dependency set and the CI image). Instead it *consumes* a
  ``Possible-Duplicates: key@score, …`` *extra* line, written upstream by the similarity
  scan that owns ``publib`` (the scraper), and surfaces those candidates as a warning in
  the PR body (no label). Like ``Replaces:``, the marker is stripped from the stored
  entry.
- **Auth anywhere.** App-installation auth minted in-process from env
  (``PUBBOT_APP_ID`` / ``PUBBOT_PRIVATE_KEY`` / ``PUBBOT_INSTALLATION_ID``) so the job
  needs no GitHub-Actions tooling; a plain ``--token`` / ``GITHUB_TOKEN`` is the
  local/manual fallback. App env vars take precedence.

The remote interaction sits behind the :class:`StorePublisher` protocol so the
per-item loop (:func:`publish_entries`) is testable with an in-memory fake — no
network in the test suite.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Protocol

from zotero_rdf import ZoteroCollection, ZoteroItem, from_bibtex, parse_zotero_rdf
from zotero_rdf.conversion import parse_extra

from . import emit
from .zotero_bridge import to_store_entries

if TYPE_CHECKING:
    from .emit import EmittedPair
    from .zotero_bridge import StoreEntry

#: ``extra`` marker lines that are *operational* signals for the publisher, not
#: publication data — read off the raw item and stripped from every stored entry.
#: ``Replaces:`` declares a rename (this item supersedes an existing entry under a
#: different citekey — a metadata edit that changes an unpinned key would otherwise read
#: as a new add, leaving the old entry stale). ``Possible-Duplicates:`` carries the
#: similarity scan's candidate matches (``citekey@score`` pairs, computed upstream where
#: ``publib`` lives — never in this CLI) so the PR can surface them for review. Both are
#: co-located with the item in Zotero, so they ride the RDF/extra round-trip.
_MARKER_LINE_RE = re.compile(r"(?i)^\s*(replaces|possible-duplicates)\s*:")


# --------------------------------------------------------------------------- #
# The remote, behind a protocol (real impl: GitHub; tests: an in-memory fake)
# --------------------------------------------------------------------------- #
class StorePublisher(Protocol):
    """The narrow slice of remote-repo operations the publish loop needs."""

    def existing_citekeys(self) -> set[str]:
        """Every citekey already in the store (one recursive ``entries/`` listing)."""

    def stored_pair(self, citekey: str) -> tuple[str, str] | None:
        """The stored ``(bib_text, meta_text)`` for an in-store citekey, else ``None``.

        Used to byte-diff against a freshly emitted pair so an unchanged item is a
        no-op while a changed one becomes an update PR.
        """

    def branch_exists(self, branch: str) -> bool:
        """Whether ``refs/heads/<branch>`` already exists on the remote."""

    def create_entry_pr(
        self,
        branch: str,
        pair: EmittedPair,
        *,
        title: str,
        body: str,
        commit_message: str,
        labels: Iterable[str] = (),
    ) -> str:
        """Commit the pair on a new branch off the base and open a PR; return its URL.

        ``labels`` are applied to the opened PR, each created on the repo first if it
        does not already exist (the GitHub API rejects unknown labels)."""

    def create_rename_pr(
        self,
        branch: str,
        pair: EmittedPair,
        *,
        old_citekey: str,
        title: str,
        body: str,
        commit_message: str,
        labels: Iterable[str] = (),
    ) -> str:
        """Like :meth:`create_entry_pr` but the same commit also *deletes* the old
        entry's ``.bib`` + ``.json`` pair (looked up by ``old_citekey``), so a rename
        lands the new files and removes the superseded ones atomically."""


class GitHubStorePublisher:
    """A :class:`StorePublisher` over a real GitHub repo via the Git Data API.

    Holds a ``github.Repository.Repository`` and the base branch; every write is one
    tree → commit → ref → PR sequence with no local clone.
    """

    def __init__(self, repo, base_branch: str = "main") -> None:
        self._repo = repo
        self._base = base_branch
        self._index_cache: dict[str, tuple[str, str]] | None = None
        self._labels_seen: set[str] = set()  # labels ensured to exist this run

    def _base_sha(self) -> str:
        return self._repo.get_branch(self._base).commit.sha

    def _entry_index(self) -> dict[str, tuple[str, str]]:
        """Map ``citekey -> (bib_path, meta_path)`` from one recursive tree listing.

        Citekeys are store-unique across year shards, so the stem of each
        ``entries/**/<key>.bib`` keys its pair; the sibling ``meta/**/<key>.json``
        path is derived by swapping the prefix and suffix (the store's invariant).
        Cached for the run — the base branch is fixed while publishing, and
        ``existing_citekeys`` / ``stored_pair`` / ``create_rename_pr`` all consult it.
        """
        if self._index_cache is None:
            tree = self._repo.get_git_tree(self._base_sha(), recursive=True)
            index: dict[str, tuple[str, str]] = {}
            for el in tree.tree:
                if el.path.startswith("entries/") and el.path.endswith(".bib"):
                    bib_path = el.path
                    meta_path = "meta/" + bib_path[len("entries/") : -len(".bib")] + ".json"
                    index[Path(bib_path).stem] = (bib_path, meta_path)
            self._index_cache = index
        return self._index_cache

    def existing_citekeys(self) -> set[str]:
        return set(self._entry_index())

    def stored_pair(self, citekey: str) -> tuple[str, str] | None:
        paths = self._entry_index().get(citekey)
        if paths is None:
            return None
        bib_path, meta_path = paths
        bib = self._repo.get_contents(bib_path, ref=self._base).decoded_content.decode("utf-8")
        meta = self._repo.get_contents(meta_path, ref=self._base).decoded_content.decode("utf-8")
        return (bib, meta)

    def branch_exists(self, branch: str) -> bool:
        from github import GithubException

        try:
            self._repo.get_git_ref(f"heads/{branch}")
            return True
        except GithubException as exc:
            if exc.status == 404:
                return False
            raise

    def _pair_elements(self, pair: EmittedPair) -> list:
        from github import InputGitTreeElement

        return [
            InputGitTreeElement(
                path=pair.bib_path.as_posix(), mode="100644", type="blob", content=pair.bib_text
            ),
            InputGitTreeElement(
                path=pair.meta_path.as_posix(), mode="100644", type="blob", content=pair.meta_text
            ),
        ]

    def _ensure_label(self, name: str) -> None:
        """Create ``name`` on the repo if absent — the add-labels API rejects unknown
        labels (the web UI's auto-create is UI-only). Idempotent and cached per run; a
        ``422`` means another concurrent run already created it."""
        if name in self._labels_seen:
            return
        from github import GithubException

        try:
            self._repo.create_label(name, _label_color(name))
        except GithubException as exc:
            if exc.status != 422:  # 422 == already exists
                raise
        self._labels_seen.add(name)

    def _open_pr(self, branch, elements, *, title, body, commit_message, labels=()) -> str:
        """tree → commit → ref → PR off the base, from a ready list of tree elements."""
        base_sha = self._base_sha()
        base_tree = self._repo.get_git_tree(base_sha)
        tree = self._repo.create_git_tree(elements, base_tree)
        parent = self._repo.get_git_commit(base_sha)
        commit = self._repo.create_git_commit(commit_message, tree, [parent])
        self._repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
        pr = self._repo.create_pull(title=title, body=body, head=branch, base=self._base)
        labels = list(labels)
        if labels:
            for name in labels:
                self._ensure_label(name)
            pr.add_to_labels(*labels)
        return pr.html_url

    def create_entry_pr(
        self, branch, pair, *, title, body, commit_message, labels=()
    ) -> str:
        return self._open_pr(
            branch, self._pair_elements(pair), title=title, body=body,
            commit_message=commit_message, labels=labels,
        )

    def create_rename_pr(
        self, branch, pair, *, old_citekey, title, body, commit_message, labels=()
    ) -> str:
        from github import InputGitTreeElement

        elements = self._pair_elements(pair)
        new_paths = {pair.bib_path.as_posix(), pair.meta_path.as_posix()}
        old = self._entry_index().get(old_citekey)
        if old:
            # A tree element with sha=None deletes the path. Skip any old path that the
            # new pair already overwrites (so we never delete-then-fail to re-add).
            for path in old:
                if path not in new_paths:
                    elements.append(
                        InputGitTreeElement(path=path, mode="100644", type="blob", sha=None)
                    )
        return self._open_pr(
            branch, elements, title=title, body=body,
            commit_message=commit_message, labels=labels,
        )


def make_publisher(
    repo_name: str,
    *,
    base_branch: str = "main",
    token: str | None = None,
    app_id: str | None = None,
    private_key: str | None = None,
    installation_id: str | None = None,
) -> GitHubStorePublisher:
    """Build a :class:`GitHubStorePublisher`, minting App-installation auth in-process.

    App credentials (all three of ``app_id`` / ``private_key`` / ``installation_id``)
    take precedence; otherwise a plain ``token``. Raises ``SystemExit`` with an
    actionable message if neither is fully supplied. ``github`` is imported lazily so
    the module (and its pure publish loop) import without PyGithub installed.
    """
    from github import Auth, Github

    if app_id and private_key and installation_id:
        auth = Auth.AppAuth(int(app_id), private_key).get_installation_auth(int(installation_id))
    elif token:
        auth = Auth.Token(token)
    else:
        raise SystemExit(
            "no credentials: set PUBBOT_APP_ID + PUBBOT_PRIVATE_KEY + "
            "PUBBOT_INSTALLATION_ID, or pass --token / set GITHUB_TOKEN"
        )
    return GitHubStorePublisher(Github(auth=auth).get_repo(repo_name), base_branch)


# --------------------------------------------------------------------------- #
# PR presentation
# --------------------------------------------------------------------------- #
def _authors(bib_text: str) -> str:
    """A human-readable author list from a canonical ``.bib`` (``""`` if none)."""
    items = from_bibtex(bib_text)
    if not items or not items[0].creators:
        return ""
    return ", ".join(str(c) for c in items[0].creators)


def build_pr_title(bib_text: str, citekey: str, *, action: str = "New") -> str:
    """A reviewer-facing PR title: ``<action> Publication: "<title>" by <author> et al.``.

    ``action`` is ``"New"`` for an added item and ``"Update"`` for a changed one. Title
    and authors come from the canonical bib. The byline is the first creator's last name
    (full name if no last name), with ``et al.`` appended only when there is more than
    one creator. Falls back to the citekey if the bib carries no title.
    """
    items = from_bibtex(bib_text)
    if not items:
        return f"{action} Publication: {citekey}"
    item = items[0]
    title = item.title or citekey
    creators = item.creators
    if not creators:
        return f'{action} Publication: "{title}"'
    first = creators[0]
    byline = first.lastName or f"{first.firstName or ''} {first.lastName or ''}".strip() or "?"
    suffix = " et al." if len(creators) > 1 else ""
    return f'{action} Publication: "{title}" by {byline}{suffix}'


def _item_meta(bib_text: str, *, year: str) -> tuple[str, str, str, str]:
    """``(title, itemType, year, authors)`` for the header, all from the canonical bib."""
    items = from_bibtex(bib_text)
    item = items[0] if items else None
    title = (item.title if item else "") or "—"
    item_type = (item.itemType if item else "") or "—"
    return title, item_type, year, _authors(bib_text) or "—"


def _details(summary: str, content: str, *, open: bool = False) -> str:
    """A collapsible ``<details>`` block (rendered by GitHub's Markdown)."""
    tag = "<details open>" if open else "<details>"
    return f"{tag}<summary><b>{summary}</b></summary>\n\n{content}\n\n</details>"


def _diff_block(summary: str, old: str, new: str, *, open: bool = False) -> str:
    """A collapsible unified-diff block; ``(no change)`` when the two texts are equal."""
    diff = "\n".join(
        difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="")
    )
    return _details(summary, f"```diff\n{diff or '(no change)'}\n```", open=open)


def _duplicates_block(duplicates: list[tuple[str, float | None]]) -> str:
    """A prominent blockquote listing the scan's possible-duplicate candidates.

    Each line is the candidate's citekey and, when the scan supplied one, its similarity
    score (0–1, two decimals). Best-first, as the scan emitted them.
    """
    lines = ["> ⚠️ **Possible duplicates already in the store** — check before merging:"]
    for citekey, score in duplicates:
        suffix = f" — score {score:.2f}" if score is not None else ""
        lines.append(f"> - `{citekey}`{suffix}")
    return "\n".join(lines)


def _checklist(
    action: str, *, citekey: str, replaces: str | None, has_duplicates: bool
) -> list[str]:
    """The reviewer to-do items for an add / update / rename PR."""
    dup = (
        ["Confirm this is **not** one of the possible duplicates listed above"]
        if has_duplicates
        else []
    )
    if action == "Update":
        return ["The change(s) shown above are intended and correct", *dup]
    base = [
        "This is a genuine publication authored by a member of an AI Center group "
        "(**reject** if not)",
        "Title, authors, year, and venue are correct",
        "Group association(s) are correct",
    ]
    if action == "Rename" and replaces:
        return [
            f"`{replaces}` and `{citekey}` are the same publication (the old entry is removed)",
            *base,
            *dup,
        ]
    return [*base, "Not a duplicate of an existing entry under a different citekey", *dup]


def build_pr_body(
    store_entry: StoreEntry,
    pair: EmittedPair,
    *,
    action: str = "New",
    old_pair: tuple[str, str] | None = None,
    replaces: str | None = None,
    duplicates: list[tuple[str, float | None]] | None = None,
) -> str:
    """The reviewer-facing PR body: a self-contained view of what the PR does.

    Carries an inline header (title, authors, type, groups, citekey — all from the
    canonical bib + ``custom.groups``, so a multi-group item surfaces all its PIs), a
    per-action review checklist, and the BibTeX in a collapsible block. ``action`` is
    ``"New"`` / ``"Update"`` / ``"Rename"``:

    * **Update** — ``old_pair`` is the stored ``(bib, meta)``; a collapsible BibTeX diff
      (open) and sidecar diff (collapsed) show exactly what changed.
    * **Rename** — ``replaces`` names the superseded citekey whose pair this PR deletes,
      shown as an ``old → new`` swap so the reviewer sees the supersession.
    * ``duplicates`` — ``(citekey, score)`` candidates from the upstream similarity scan
      (the ``Possible-Duplicates:`` marker), surfaced as a prominent warning + a checklist
      item so a likely re-submission is caught before merge.
    """
    year = pair.bib_path.parent.name
    title, item_type, year, authors = _item_meta(pair.bib_text, year=year)
    groups = store_entry.sidecar.get("custom", {}).get("groups", [])
    citekey = store_entry.citekey

    lines: list[str] = []
    if action == "Rename" and replaces:
        lines += [
            "### Rename Publication",
            "",
            f"`{replaces}` → **`{citekey}`**",
            "",
            f"**Supersedes** `{replaces}` — its `.bib` + `.json` are deleted in this PR.",
        ]
    else:
        lines += [f"### {action} Publication", "", f'**"{title}"** — {year}']

    lines += [
        "",
        f"**Authors:** {authors}",
        f"**Type:** `{item_type}` • "
        f"**Groups:** {', '.join(groups) if groups else '—'} • "
        f"**Citekey:** `{citekey}`",
    ]

    if duplicates:
        lines += ["", _duplicates_block(duplicates)]

    if action == "Update" and old_pair is not None:
        old_bib, old_meta = old_pair
        lines += [
            "",
            "#### What changed",
            _diff_block("BibTeX diff", old_bib, pair.bib_text, open=True),
            "",
            _diff_block("Sidecar diff", old_meta, pair.meta_text),
        ]

    lines += ["", "#### How to review"]
    lines += [
        f"- [ ] {item}"
        for item in _checklist(
            action, citekey=citekey, replaces=replaces, has_duplicates=bool(duplicates)
        )
    ]

    bib_label = "Full BibTeX (after)" if action == "Update" else "BibTeX"
    lines += ["", _details(bib_label, f"```bibtex\n{pair.bib_text.rstrip()}\n```")]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Labels — filterable axes on each PR (group slug, item type, op kind)
# --------------------------------------------------------------------------- #
#: A fixed hex color per label axis so the PR list reads at a glance (no leading ``#``).
#: Axis-less labels (``new`` / ``update`` / ``rename``) take the ``action`` color.
_LABEL_COLORS = {"action": "5319e7", "type": "0e8a16", "group": "1d76db"}


def _label_color(name: str) -> str:
    """The color for a label, keyed off its ``<axis>:`` prefix (action labels are bare)."""
    axis = name.split(":", 1)[0] if ":" in name else "action"
    return _LABEL_COLORS.get(axis, _LABEL_COLORS["action"])


def _labels_for(action: str, bib_text: str, groups: Iterable[str]) -> list[str]:
    """The filter labels for a PR: the op kind (``new`` / ``update`` / ``rename``), the
    ``type:<itemType>``, and one ``group:<slug>`` per owning group."""
    items = from_bibtex(bib_text)
    labels = [action.lower()]
    if items and items[0].itemType:
        labels.append(f"type:{items[0].itemType}")
    labels += [f"group:{g}" for g in groups]
    return labels


# --------------------------------------------------------------------------- #
# Assembling publish items from the RDF (citekey identity + rename/pin signals)
# --------------------------------------------------------------------------- #
@dataclass
class PublishItem:
    """A store entry plus the two identity signals read off its source Zotero item.

    * ``replaces`` — the citekey this item supersedes, from a ``Replaces: <key>`` line
      in the Zotero ``extra`` (``None`` if absent). The explicit, operator-controlled
      rename: it is stripped from the stored entry (operational, not publication data).
    * ``pinned`` — whether the citekey is explicit/pinned (a ``Citation Key:`` in extra
      or a ``citationKey`` field) rather than auto-generated. An auto-generated key
      drifts when author/title/year are edited, so the warn guard surfaces these.
    * ``duplicates`` — ``(citekey, score)`` candidates from a ``Possible-Duplicates:``
      extra line, written upstream by the similarity scan (where ``publib`` lives). Like
      ``replaces`` it is operational, stripped from the stored entry, and surfaced only
      in the PR.
    """

    entry: StoreEntry
    replaces: str | None = None
    pinned: bool = True
    duplicates: list[tuple[str, float | None]] = field(default_factory=list)


def _is_pinned(item: ZoteroItem) -> bool:
    """Whether ``item``'s citekey is explicit (pinned) rather than auto-generated."""
    if getattr(item, "citationKey", None):
        return True
    return any(k.lower() == "citation key" for k in parse_extra(item.extra or ""))


def _extract_replaces(extra: str | None) -> str | None:
    """The superseded citekey from a ``Replaces: <key>`` line in ``extra``, else ``None``."""
    for key, value in parse_extra(extra or "").items():
        if key.lower() == "replaces" and value:
            return value
    return None


def _extract_duplicates(extra: str | None) -> list[tuple[str, float | None]]:
    """Candidate duplicates from a ``Possible-Duplicates: key@score, key2@score2`` line.

    Each comma-separated entry is a citekey, optionally suffixed ``@<score>`` (a float the
    scan attached); a malformed/absent score yields ``None``. Order is preserved (the scan
    emits best-first). Returns ``[]`` when the line is absent.
    """
    for key, value in parse_extra(extra or "").items():
        if key.lower() == "possible-duplicates" and value:
            out: list[tuple[str, float | None]] = []
            for chunk in value.split(","):
                citekey, _, raw_score = chunk.strip().partition("@")
                citekey = citekey.strip()
                if not citekey:
                    continue
                try:
                    score = float(raw_score) if raw_score.strip() else None
                except ValueError:
                    score = None
                out.append((citekey, score))
            return out
    return []


def _strip_markers(extra: str | None) -> str | None:
    """``extra`` without its operational marker line(s) (``Replaces:`` /
    ``Possible-Duplicates:``) — they never enter the store."""
    if not extra:
        return extra
    return "\n".join(ln for ln in extra.splitlines() if not _MARKER_LINE_RE.match(ln))


def build_publish_items(
    items: Iterable[ZoteroItem], collections: Iterable[ZoteroCollection] | None = None
) -> list[PublishItem]:
    """RDF ``items`` (+ collections) → ``PublishItem``\\ s, one per item, in order.

    Reads each item's ``Replaces:`` / ``Possible-Duplicates:`` markers and pin status
    *before* serialization, strips the operational markers from a copy so they never land
    in the store, then runs the normal :func:`to_store_entries`. ``to_store_entries``
    preserves input order, so the per-item signals zip back onto the resulting entries.
    """
    cleaned: list[ZoteroItem] = []
    signals: list[tuple[str | None, bool, list[tuple[str, float | None]]]] = []
    for item in items:
        signals.append(
            (_extract_replaces(item.extra), _is_pinned(item), _extract_duplicates(item.extra))
        )
        copied = copy.deepcopy(item)
        copied.extra = _strip_markers(copied.extra)
        cleaned.append(copied)
    entries = to_store_entries(cleaned, collections)
    return [
        PublishItem(entry, replaces=replaces, pinned=pinned, duplicates=duplicates)
        for entry, (replaces, pinned, duplicates) in zip(entries, signals)
    ]


# --------------------------------------------------------------------------- #
# The per-item loop (pure; drives any StorePublisher)
# --------------------------------------------------------------------------- #
@dataclass
class PublishSummary:
    """Outcome of one publish run, for the closing report."""

    created: list[tuple[str, str]] = field(default_factory=list)  # (citekey, pr_url)
    updated: list[tuple[str, str]] = field(default_factory=list)  # (citekey, pr_url)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (citekey, pr_url)
    skipped_unchanged: list[str] = field(default_factory=list)
    skipped_branch_exists: list[str] = field(default_factory=list)
    rename_target_missing: list[tuple[str, str]] = field(default_factory=list)  # (citekey, old)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (citekey, message)

    def opened(self) -> int:
        """How many PRs this run has opened so far (adds + updates + renames)."""
        return len(self.created) + len(self.updated) + len(self.renamed)


def publish_entries(
    publisher: StorePublisher,
    items: Iterable[PublishItem | StoreEntry],
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> PublishSummary:
    """Open one add/update/rename PR per item; skip unchanged items and existing branches.

    The store's citekey set is fetched once (a single recursive ``entries/`` listing).
    Per item the canonical pair is emitted, then, in precedence order:

    - **Rename** — the item declares ``Replaces: <old>`` and ``<old>`` is in the store
      under a different citekey → an *update+delete* on branch ``rename/<citekey>``: the
      new pair is written and ``<old>``'s pair removed in one commit. (If ``<old>`` is
      not in the store, the marker is recorded in ``rename_target_missing`` and the item
      falls through to the add/update path.)
    - **Not in store** → an *add* on branch ``<citekey>`` (PR title ``New Publication``).
    - **In store, pair byte-identical** to what is stored → skip (a true no-op).
    - **In store, pair differs** → an *update* on branch ``update/<citekey>``; the same
      paths are overwritten by the tree commit.

    A pre-existing branch (an open/abandoned PR for that op) is skipped. ``limit`` caps
    the number of PRs *opened* this run (adds + updates + renames) — skips don't count,
    so a re-run continues through the remaining delta. Plain :class:`StoreEntry` inputs
    are accepted too (treated as having no rename marker). Errors on one item are
    recorded and the run continues.
    """
    summary = PublishSummary()
    in_store = publisher.existing_citekeys()

    for item in items:
        pub_item = item if isinstance(item, PublishItem) else PublishItem(item)
        store_entry = pub_item.entry
        citekey = store_entry.citekey
        if limit is not None and summary.opened() >= limit:
            break
        try:
            pair = emit.emit_pair(store_entry)
            old = pub_item.replaces
            dups = pub_item.duplicates

            # Rename takes precedence: an explicit Replaces marker whose target is in the
            # store supersedes it, even when the new citekey itself is brand new.
            if old and old != citekey:
                if old not in in_store:
                    summary.rename_target_missing.append((citekey, old))
                    # fall through to the normal add/update handling below
                else:
                    branch = f"rename/{citekey}"
                    if publisher.branch_exists(branch):
                        summary.skipped_branch_exists.append(citekey)
                        continue
                    if dry_run:
                        summary.renamed.append((citekey, "(dry-run)"))
                        continue
                    groups = store_entry.sidecar.get("custom", {}).get("groups", [])
                    url = publisher.create_rename_pr(
                        branch,
                        pair,
                        old_citekey=old,
                        title=build_pr_title(pair.bib_text, citekey, action="Update"),
                        body=build_pr_body(
                            store_entry, pair, action="Rename", replaces=old, duplicates=dups
                        ),
                        commit_message=f"feat: rename {old} -> {citekey}",
                        labels=_labels_for("rename", pair.bib_text, groups),
                    )
                    summary.renamed.append((citekey, url))
                    continue

            if citekey in in_store:
                old_pair = publisher.stored_pair(citekey)
                if old_pair == (pair.bib_text, pair.meta_text):
                    summary.skipped_unchanged.append(citekey)
                    continue
                action, branch, verb, bucket = "Update", f"update/{citekey}", "update", summary.updated
            else:
                old_pair, action, branch, verb, bucket = None, "New", citekey, "add", summary.created

            if publisher.branch_exists(branch):
                summary.skipped_branch_exists.append(citekey)
                continue
            if dry_run:
                bucket.append((citekey, "(dry-run)"))
                continue
            groups = store_entry.sidecar.get("custom", {}).get("groups", [])
            url = publisher.create_entry_pr(
                branch,
                pair,
                title=build_pr_title(pair.bib_text, citekey, action=action),
                body=build_pr_body(
                    store_entry, pair, action=action, old_pair=old_pair, duplicates=dups
                ),
                commit_message=f"feat: {verb} {citekey}",
                labels=_labels_for("new" if action == "New" else "update", pair.bib_text, groups),
            )
            bucket.append((citekey, url))
        except Exception as exc:  # one bad item must not sink the whole run
            summary.errors.append((citekey, str(exc)))

    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Open one branch + PR per publication against the store repo."
    )
    ap.add_argument("rdf", type=Path, help="Zotero RDF export (groups carried as collections)")
    ap.add_argument("--repo", required=True, help="OWNER/REPO of the store repository")
    ap.add_argument("--base", default="main", help="base branch to open PRs against (default: main)")
    ap.add_argument(
        "--limit", type=int, default=None, help="cap PRs opened this run (adds + updates + renames)"
    )
    ap.add_argument("--dry-run", action="store_true", help="compute + log, no commit/PR")
    ap.add_argument("--token", default=None, help="plain token (else GITHUB_TOKEN; App env vars win)")
    args = ap.parse_args()

    items, cols = parse_zotero_rdf(str(args.rdf))
    pub_items = build_publish_items(items, cols)
    print(f"source: {len(items)} items → {len(pub_items)} store entries")

    # Warn-only pin guard: an auto-generated citekey drifts when author/title/year are
    # edited (silently orphaning the old entry). Surface these so they can be pinned.
    unpinned = [pi.entry.citekey for pi in pub_items if not pi.pinned]
    if unpinned:
        print(
            f"⚠ {len(unpinned)} item(s) have auto-generated (unpinned) citation keys — "
            "these drift on edit; pin them in Zotero (Better BibTeX → Pin BibTeX key):"
        )
        for citekey in unpinned:
            print(f"    - {citekey}")

    publisher = make_publisher(
        args.repo,
        base_branch=args.base,
        token=args.token or os.environ.get("GITHUB_TOKEN"),
        app_id=os.environ.get("PUBBOT_APP_ID"),
        private_key=os.environ.get("PUBBOT_PRIVATE_KEY"),
        installation_id=os.environ.get("PUBBOT_INSTALLATION_ID"),
    )

    summary = publish_entries(publisher, pub_items, limit=args.limit, dry_run=args.dry_run)

    verb = "would open" if args.dry_run else "opened"
    print(
        f"{verb} {len(summary.created)} add + {len(summary.updated)} update + "
        f"{len(summary.renamed)} rename PR(s); "
        f"skipped {len(summary.skipped_unchanged)} (unchanged), "
        f"{len(summary.skipped_branch_exists)} (branch exists); "
        f"{len(summary.errors)} error(s)"
    )
    for citekey, url in summary.created:
        print(f"  + {citekey}  {url}")
    for citekey, url in summary.updated:
        print(f"  ~ {citekey}  {url}")
    for citekey, url in summary.renamed:
        print(f"  » {citekey}  {url}")
    for citekey, old in summary.rename_target_missing:
        print(f"  ::warning:: {citekey}: Replaces target '{old}' not in store — treated as add/update")
    for citekey, message in summary.errors:
        print(f"  ::error:: {citekey}: {message}")
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
