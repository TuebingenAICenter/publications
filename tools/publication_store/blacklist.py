"""``pubstore-blacklist`` — the "stop re-proposing these" RDF for the scraper.

The scraper's incremental-dedup consumer (``publib.new_publications(scraped, store,
blacklist)``) drops every scraped item that matches — by similarity ≥ cutoff — anything in
``store`` *or* ``blacklist``. ``store`` is already producible (``pubstore-compile --only
rdf`` → ``library.rdf``); this CLI builds the **blacklist** side: the items a human has
already *adjudicated away*, so the monthly run stops re-proposing them under a drifted
citekey that ``pubstore-publish``'s exact-citekey skip can't catch.

Four sources, all reconstructed identically (``.bib`` text → :func:`from_bibtex` →
``ZoteroItem``), unioned, then :func:`export_to_rdf`:

1. **open** ``new`` PRs — in-flight, so a paper with an open PR isn't re-proposed under a
   drifted key;
2. **closed-unmerged** ``new`` PRs — deliberate rejections (safe because ``pubstore-sweep``
   defaults to ``--on-expiry accept``: a *timed-out* PR is merged, not closed, so a
   closed-unmerged PR is a real human "NO");
3. **store-file deletions** over git history — the post-merge "undo": a citekey present
   somewhere in history but absent from ``entries/`` at HEAD;
4. **hand-curated** entries — a ``blacklist/*.bib`` file the store maintains by hand (plus
   any local ``--include`` paths).

The op-kind filter is ``new`` only (default): ``update`` / ``rename`` PRs presuppose the
item is already in the store, which ``library.rdf`` already excludes.

Like ``publish``/``sweep`` the remote sits behind a :class:`Blacklister` protocol so the
pure core (:func:`collect_blacklist`) is testable with an in-memory fake — no network in
the test suite. Unlike them, the real impl also ``git clone``\\ s the store: all *content*
(PR ``.bib``\\ s **and** deletion history) is read from that one clone, and the GitHub API
is used only for PR *metadata* (op-kind label, merged-vs-closed) — the part with no git
representation. That keeps the run off the per-PR REST fan-out that tripped GitHub's
secondary rate limits. It is a once-a-month cost, so it shells out to the ``git`` binary —
no new Python dependency (see :class:`GitHubBlacklister` for the two-pass split).

Self-dedup is deliberately omitted: duplicates across sources only shrink nothing and
``new_publications`` dedups against the blacklist at consumption time anyway, so multi-
source repeats are harmless — and pulling ``publib`` in for a cosmetic collapse would
break the "``publib`` stays out of the store's dependency set" invariant.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from zotero_rdf import ZoteroItem, export_to_rdf, from_bibtex

#: The ``pubstore-publish`` op-kind labels that mark a PR as a *new-publication* PR — the
#: only kind that adds anything to the blacklist (``update`` / ``rename`` presuppose the
#: item is already in the store). Overridable via ``--label``.
DEFAULT_OP_LABELS = ("new",)

#: In-repo path (a glob, relative to the store root) of the hand-curated blacklist bib(s).
DEFAULT_CURATED_GLOB = "blacklist/*.bib"

_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _stderr(exc: subprocess.CalledProcessError) -> str:
    """First line of a failed git command's stderr, for a compact error record."""
    text = (exc.stderr or "").strip()
    return text.splitlines()[0] if text else f"git exited {exc.returncode}"


# --------------------------------------------------------------------------- #
# The sources, behind a protocol (real impl: GitHub + clone; tests: a fake)
# --------------------------------------------------------------------------- #
class Blacklister(Protocol):
    """The four bib-text sources the pure core unions into the blacklist."""

    def open_pr_bibs(self) -> list[str]:
        """``.bib`` text committed on open ``new`` PRs (read at each PR head)."""

    def closed_pr_bibs(self) -> list[str]:
        """``.bib`` text committed on closed-unmerged ``new`` PRs (deliberate rejections)."""

    def deleted_bibs(self) -> list[str]:
        """``.bib`` text of entries whose citekey is gone from ``entries/`` at HEAD."""

    def curated_bibs(self) -> list[str]:
        """``.bib`` text a human maintains by hand (in-repo glob + local ``--include``)."""


class GitHubBlacklister:
    """A :class:`Blacklister` over a real store repo, reading **all content from one
    local clone** and hitting the GitHub API only for PR *metadata*.

    The insight the design turns on: a PR's *content* (the ``.bib`` it adds) is
    recoverable from git — ``refs/pull/N/head`` stays fetchable after branch deletion —
    while a PR's *metadata* (its op-kind label, merged-vs-closed) has no git
    representation and must come from the API. So each PR scan is two passes:

    * a **metadata pass** — one paginated ``get_pulls(state=…)`` listing whose page
      payload already carries ``number``/``labels``/``merged_at``/``head.sha``/``base.sha``
      inline, so it filters to in-scope PRs with *zero* per-PR REST calls; and
    * a **content pass** — a local ``git diff base…head`` + ``git show`` against the
      shared clone, replacing the old per-PR ``get_files()`` + ``get_contents()`` fan-out
      (``≥2`` REST calls each, which tripped GitHub's secondary rate limits).

    Holds a ``github.Repository.Repository`` (metadata pass), plus the ``OWNER/REPO`` name
    and a bearer token used to clone/fetch over HTTPS. The clone is lazy and shared across
    all four sources. Per-PR read failures (a GC'd head, an orphaned base) are collected
    in :attr:`errors` and skipped, never sinking the run.
    """

    def __init__(
        self,
        repo,
        *,
        repo_name: str,
        token: str,
        op_labels: tuple[str, ...] = DEFAULT_OP_LABELS,
        include: Iterable[str] = (),
        curated_glob: str = DEFAULT_CURATED_GLOB,
    ) -> None:
        self._repo = repo
        self._repo_name = repo_name
        self._token = token
        self._op_labels = set(op_labels)
        self._include = [Path(p) for p in include]
        self._curated_glob = curated_glob
        self._clone_dir: Path | None = None
        #: (source, message) for PRs skipped mid-run; merged into the run's
        #: :class:`BlacklistSummary` by :func:`collect_blacklist` (duck-typed).
        self.errors: list[tuple[str, str]] = []

    # -- PR scans (metadata: API; content: local git) ----------------------- #
    def _pr_meta(self, state: str, *, unmerged_only: bool) -> list[tuple[int, str, str]]:
        """``(number, head_sha, base_sha)`` for in-scope op-kind PRs in ``state``.

        A single ``get_pulls`` listing; both SHAs and the label/merged filter come off the
        page payload, so this makes no per-PR call.
        """
        meta: list[tuple[int, str, str]] = []
        for pr in self._repo.get_pulls(state=state):
            labels = {label.name for label in pr.labels}
            if not (labels & self._op_labels):
                continue
            if unmerged_only and pr.merged_at is not None:
                continue
            meta.append((pr.number, pr.head.sha, pr.base.sha))
        return meta

    def _fetch_pr_heads(self, numbers: list[int], *, source: str) -> set[int]:
        """Fetch the given PRs' head commits into the clone.

        One batched ``git fetch`` when every ``refs/pull/N/head`` resolves; on failure
        (e.g. a ref GitHub has since GC'd) fall back to per-PR fetches so one bad ref only
        drops its own PR. Returns the set of PR numbers whose head is now local.
        """
        if not numbers:
            return set()
        refspecs = [f"refs/pull/{n}/head:refs/pr/{n}" for n in numbers]
        try:
            self._git("fetch", "--quiet", "origin", *refspecs)
            return set(numbers)
        except subprocess.CalledProcessError:
            fetched: set[int] = set()
            for n in numbers:
                try:
                    self._git("fetch", "--quiet", "origin", f"refs/pull/{n}/head:refs/pr/{n}")
                    fetched.add(n)
                except subprocess.CalledProcessError as exc:
                    self.errors.append((source, f"PR #{n}: head unfetchable: {_stderr(exc)}"))
            return fetched

    def _pr_diff_bibs(self, head_sha: str, base_sha: str) -> list[str]:
        """``.bib`` text the PR *introduces* — its ``entries/**/*.bib`` additions/mods.

        ⚠️ A PR head commit carries the whole repo tree, so we diff **against base**, not
        list the head tree. Three-dot ``base…head`` diffs the merge-base→head, isolating
        the PR's own changes; ``--diff-filter=AM`` keeps additions/modifications and drops
        deletions — the faithful local twin of the old ``get_files()`` + ``status !=
        "removed"`` filter. ``--no-renames`` is essential: otherwise a PR that deletes one
        entry and adds a *similar* one has the addition folded into a rename (``R``) and
        dropped, losing a real blacklist item (GitHub's ``get_files`` likewise reported
        such a file "renamed", not "removed", so the old path read it too). Raises
        ``CalledProcessError`` if ``base_sha`` is unreachable (orphaned by a force-push);
        the caller records that PR and moves on.
        """
        names = self._git(
            "diff", "--name-only", "--no-renames", "--diff-filter=AM",
            f"{base_sha}...{head_sha}", "--", "entries",
        )
        out: list[str] = []
        for path in names.splitlines():
            path = path.strip()
            if path.startswith("entries/") and path.endswith(".bib"):
                out.append(self._git("show", f"{head_sha}:{path}"))
        return out

    def _pr_bibs(self, state: str, *, unmerged_only: bool, source: str) -> list[str]:
        meta = self._pr_meta(state, unmerged_only=unmerged_only)
        fetched = self._fetch_pr_heads([n for n, _, _ in meta], source=source)
        out: list[str] = []
        for number, head_sha, base_sha in meta:
            if number not in fetched:
                continue  # head unfetchable — already recorded in errors
            try:
                out.extend(self._pr_diff_bibs(head_sha, base_sha))
            except subprocess.CalledProcessError as exc:  # orphaned base, etc.
                self.errors.append((source, f"PR #{number}: {_stderr(exc)}"))
        return out

    def open_pr_bibs(self) -> list[str]:
        return self._pr_bibs("open", unmerged_only=False, source="open_prs")

    def closed_pr_bibs(self) -> list[str]:
        return self._pr_bibs("closed", unmerged_only=True, source="closed_prs")

    # -- clone-backed scans -------------------------------------------------- #
    def _clone(self) -> Path:
        """Full clone of the store (all history, full blobs), shared by every source.

        Shallow would drop the deletion history the store-file scan needs. Full blobs
        (no ``--filter=blob:none``) are the deliberate choice: the store is tiny ``.bib``/
        ``.json`` text, and a blobless clone would re-fetch each blob over the network on
        every ``git show`` — defeating the point of reading content locally.
        """
        if self._clone_dir is None:
            dest = Path(tempfile.mkdtemp(prefix="pubstore-blacklist-"))
            url = f"https://x-access-token:{self._token}@github.com/{self._repo_name}.git"
            subprocess.run(
                ["git", "clone", "--quiet", url, str(dest)],
                check=True,
                capture_output=True,
            )
            self._clone_dir = dest
        return self._clone_dir

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self._clone()), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def deleted_bibs(self) -> list[str]:
        # citekeys still present at HEAD — a delete-then-re-add (or a rename's survivor)
        # must NOT be blacklisted.
        present = {
            Path(p).stem
            for p in self._git("ls-files", "entries").splitlines()
            if p.endswith(".bib")
        }
        # (commit, deleted_path) newest-first; %H on its own line, then the deleted paths.
        log = self._git(
            "log", "--diff-filter=D", "--name-only", "--pretty=format:%H", "--", "entries"
        )
        commit: str | None = None
        seen: set[str] = set()  # dedup by citekey; newest deletion wins
        out: list[str] = []
        for line in log.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.fullmatch(r"[0-9a-f]{40}", line):
                commit = line
                continue
            if not (line.startswith("entries/") and line.endswith(".bib")):
                continue
            citekey = Path(line).stem
            if citekey in present or citekey in seen:
                continue
            seen.add(citekey)
            # The blob lives at the deletion commit's parent.
            out.append(self._git("show", f"{commit}^:{line}"))
        return out

    def curated_bibs(self) -> list[str]:
        out: list[str] = []
        clone = self._clone()
        for path in sorted(clone.glob(self._curated_glob)):
            out.append(path.read_text(encoding="utf-8"))
        for path in self._include:
            out.append(path.read_text(encoding="utf-8"))
        return out


# --------------------------------------------------------------------------- #
# Pure core (drives any Blacklister)
# --------------------------------------------------------------------------- #
@dataclass
class BlacklistSummary:
    """Per-source counts + parse errors for the closing report."""

    open_prs: int = 0
    closed_prs: int = 0
    deletions: int = 0
    curated: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (source, message)

    def total(self) -> int:
        return self.open_prs + self.closed_prs + self.deletions + self.curated


#: Human-readable ``Blacklist Reason:`` value stamped into each item's ``extra`` field,
#: keyed by the source that produced it — so a reviewer opening ``blacklist.rdf`` in Zotero
#: can see *why* an item is suppressed (rejected PR vs deletion vs …) without re-deriving it.
_BLACKLIST_REASON = {
    "open_prs": "open new-publication PR",
    "closed_prs": "closed (rejected) new-publication PR",
    "deletions": "deleted from the store",
    "curated": "hand-curated blacklist entry",
}


def _stamp_reason(item: ZoteroItem, reason: str) -> None:
    """Append a ``Blacklist Reason: <reason>`` line to ``item.extra``.

    Uses Zotero's ``Label: value`` extra convention (so it round-trips and shows in the
    GUI) and preserves any existing ``extra`` content rather than overwriting it. Chosen
    over a tag deliberately: a tag would blend into the item's real tag set.
    """
    line = f"Blacklist Reason: {reason}"
    item.extra = f"{item.extra}\n{line}" if item.extra else line


def _year(item: ZoteroItem) -> int | None:
    """First 4-digit year in ``item.date``, or ``None`` when undated/unparseable."""
    if not item.date:
        return None
    m = _YEAR_RE.search(item.date)
    return int(m.group(1)) if m else None


def _in_year_range(item: ZoteroItem, since_year: int | None) -> bool:
    """Keep undated items always; otherwise keep iff year ≥ ``since_year``.

    Mirrors the scraper's ``in_year_range`` recency semantics so the blacklist only spans
    the same window ``new_publications`` ever sees — keeping it ~constant-size over time.
    """
    if since_year is None:
        return True
    year = _year(item)
    return year is None or year >= since_year


def collect_blacklist(
    src: Blacklister, *, since_year: int | None = None
) -> tuple[list[ZoteroItem], BlacklistSummary]:
    """Union the four sources into blacklist items + a per-source :class:`BlacklistSummary`.

    Each source's bibs are parsed independently: one unparseable bib is recorded in
    ``summary.errors`` (tagged with its source) and skipped — it never sinks the run. Each
    surviving item is stamped with a ``Blacklist Reason:`` line in its ``extra`` naming the
    source. An optional ``since_year`` prunes items older than that publication year
    (undated kept). Order is source-by-source, items in source order; no self-dedup (see
    module docstring).
    """
    summary = BlacklistSummary()
    items: list[ZoteroItem] = []

    for source, bibs in (
        ("open_prs", src.open_pr_bibs()),
        ("closed_prs", src.closed_pr_bibs()),
        ("deletions", src.deleted_bibs()),
        ("curated", src.curated_bibs()),
    ):
        count = 0
        for bib in bibs:
            try:
                parsed = from_bibtex(bib)
            except Exception as exc:  # one bad bib never sinks the run
                summary.errors.append((source, str(exc)))
                continue
            for item in parsed:
                if _in_year_range(item, since_year):
                    _stamp_reason(item, _BLACKLIST_REASON[source])
                    items.append(item)
                    count += 1
        setattr(summary, source, count)

    # Fold in any source-side read failures (a GC'd PR head, an orphaned base): the pure
    # loop above only isolates *parse* errors, but a clone-backed source can fail to even
    # produce a bib. Duck-typed so the in-memory fake (no ``errors``) is unaffected.
    summary.errors.extend(getattr(src, "errors", ()))

    return items, summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build blacklist.rdf (already-adjudicated items) from the store's "
        "PRs + git history, so the scraper stops re-proposing them."
    )
    ap.add_argument("--repo", required=True, help="OWNER/REPO of the store repository")
    ap.add_argument("--out", type=Path, default=Path("blacklist.rdf"), help="output RDF path")
    ap.add_argument(
        "--label",
        action="append",
        default=None,
        help="op-kind label that marks a blacklist-eligible PR (repeatable; "
        f"default: {', '.join(DEFAULT_OP_LABELS)})",
    )
    ap.add_argument(
        "--since-year",
        type=int,
        default=None,
        help="drop blacklist items published before this year (undated kept)",
    )
    ap.add_argument(
        "--include",
        action="append",
        default=None,
        help="extra local hand-curated .bib file to fold in (repeatable)",
    )
    ap.add_argument("--dry-run", action="store_true", help="report per-source counts, write nothing")
    ap.add_argument("--token", default=None, help="plain token (else GITHUB_TOKEN; App env vars win)")
    args = ap.parse_args()

    from ._github import github_repo, github_token

    app_creds = dict(
        app_id=os.environ.get("PUBBOT_APP_ID"),
        private_key=os.environ.get("PUBBOT_PRIVATE_KEY"),
        installation_id=os.environ.get("PUBBOT_INSTALLATION_ID"),
    )
    token = args.token or os.environ.get("GITHUB_TOKEN")
    repo = github_repo(args.repo, token=token, **app_creds)
    op_labels = tuple(args.label) if args.label else DEFAULT_OP_LABELS
    src = GitHubBlacklister(
        repo,
        repo_name=args.repo,
        token=github_token(token=token, **app_creds),
        op_labels=op_labels,
        include=args.include or (),
    )

    items, summary = collect_blacklist(src, since_year=args.since_year)

    verb = "would emit" if args.dry_run else "emitting"
    print(
        f"{verb} {summary.total()} blacklist item(s): "
        f"{summary.open_prs} open-PR + {summary.closed_prs} closed-PR + "
        f"{summary.deletions} deletion + {summary.curated} curated; "
        f"{len(summary.errors)} parse error(s)"
    )
    for source, message in summary.errors:
        print(f"  ::warning:: {source}: {message}")

    if not args.dry_run:
        export_to_rdf(items, str(args.out))
        print(f"  → wrote {args.out}")

    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
