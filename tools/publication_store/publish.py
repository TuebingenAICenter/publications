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

#: An ``extra`` line that declares this item supersedes an existing store entry under a
#: different citekey — the explicit, operator-controlled rename signal (a metadata edit
#: that changes the citekey, e.g. a year correction, would otherwise read as a new add
#: leaving the old entry stale). Co-located with the item in Zotero, so it rides the RDF.
_REPLACES_LINE_RE = re.compile(r"(?i)^\s*replaces\s*:")


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
        self, branch: str, pair: EmittedPair, *, title: str, body: str, commit_message: str
    ) -> str:
        """Commit the pair on a new branch off the base and open a PR; return its URL."""

    def create_rename_pr(
        self,
        branch: str,
        pair: EmittedPair,
        *,
        old_citekey: str,
        title: str,
        body: str,
        commit_message: str,
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

    def _open_pr(self, branch, elements, *, title, body, commit_message) -> str:
        """tree → commit → ref → PR off the base, from a ready list of tree elements."""
        base_sha = self._base_sha()
        base_tree = self._repo.get_git_tree(base_sha)
        tree = self._repo.create_git_tree(elements, base_tree)
        parent = self._repo.get_git_commit(base_sha)
        commit = self._repo.create_git_commit(commit_message, tree, [parent])
        self._repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
        pr = self._repo.create_pull(title=title, body=body, head=branch, base=self._base)
        return pr.html_url

    def create_entry_pr(
        self, branch: str, pair: EmittedPair, *, title: str, body: str, commit_message: str
    ) -> str:
        return self._open_pr(
            branch, self._pair_elements(pair), title=title, body=body, commit_message=commit_message
        )

    def create_rename_pr(
        self, branch, pair, *, old_citekey, title, body, commit_message
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
            branch, elements, title=title, body=body, commit_message=commit_message
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


def build_pr_body(store_entry: StoreEntry, bib_text: str, *, replaces: str | None = None) -> str:
    """The reviewer-facing PR body: paper title, authors, and group associations.

    Title and authors come from the canonical bib; groups come straight from the
    entry's ``custom.groups`` (so a multi-group item still surfaces all its PIs in its
    single PR). On a rename, ``replaces`` names the superseded citekey whose pair this
    PR deletes, so the reviewer sees the swap.
    """
    items = from_bibtex(bib_text)
    title = items[0].title if items else ""
    authors = _authors(bib_text)
    groups = store_entry.sidecar.get("custom", {}).get("groups", [])
    lines = [
        f"**Title:** {title or '—'}",
        f"**Authors:** {authors or '—'}",
        f"**Groups:** {', '.join(groups) if groups else '—'}",
    ]
    if replaces:
        lines.append(f"**Supersedes:** `{replaces}` (its files are removed in this PR)")
    return "\n".join(lines) + "\n"


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
    """

    entry: StoreEntry
    replaces: str | None = None
    pinned: bool = True


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


def _strip_replaces(extra: str | None) -> str | None:
    """``extra`` without its ``Replaces:`` line(s) — the marker never enters the store."""
    if not extra:
        return extra
    return "\n".join(ln for ln in extra.splitlines() if not _REPLACES_LINE_RE.match(ln))


def build_publish_items(
    items: Iterable[ZoteroItem], collections: Iterable[ZoteroCollection] | None = None
) -> list[PublishItem]:
    """RDF ``items`` (+ collections) → ``PublishItem``\\ s, one per item, in order.

    Reads each item's ``Replaces:`` and pin status *before* serialization, strips the
    ``Replaces:`` marker from a copy so it never lands in the store, then runs the
    normal :func:`to_store_entries`. ``to_store_entries`` preserves input order, so the
    per-item signals zip back onto the resulting entries.
    """
    cleaned: list[ZoteroItem] = []
    signals: list[tuple[str | None, bool]] = []
    for item in items:
        signals.append((_extract_replaces(item.extra), _is_pinned(item)))
        copied = copy.deepcopy(item)
        copied.extra = _strip_replaces(copied.extra)
        cleaned.append(copied)
    entries = to_store_entries(cleaned, collections)
    return [
        PublishItem(entry, replaces=replaces, pinned=pinned)
        for entry, (replaces, pinned) in zip(entries, signals)
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
                    url = publisher.create_rename_pr(
                        branch,
                        pair,
                        old_citekey=old,
                        title=build_pr_title(pair.bib_text, citekey, action="Update"),
                        body=build_pr_body(store_entry, pair.bib_text, replaces=old),
                        commit_message=f"feat: rename {old} -> {citekey}",
                    )
                    summary.renamed.append((citekey, url))
                    continue

            if citekey in in_store:
                if publisher.stored_pair(citekey) == (pair.bib_text, pair.meta_text):
                    summary.skipped_unchanged.append(citekey)
                    continue
                action, branch, verb, bucket = "Update", f"update/{citekey}", "update", summary.updated
            else:
                action, branch, verb, bucket = "New", citekey, "add", summary.created

            if publisher.branch_exists(branch):
                summary.skipped_branch_exists.append(citekey)
                continue
            if dry_run:
                bucket.append((citekey, "(dry-run)"))
                continue
            url = publisher.create_entry_pr(
                branch,
                pair,
                title=build_pr_title(pair.bib_text, citekey, action=action),
                body=build_pr_body(store_entry, pair.bib_text),
                commit_message=f"feat: {verb} {citekey}",
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
