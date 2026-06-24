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
- **Idempotent re-runs.** Skip an item whose citekey is already in the store (checked
  against a one-shot recursive listing of ``entries/`` — store-wide, across *any* year
  shard, since citekeys are store-unique) **or** whose branch ``<citekey>`` already
  exists (an open/abandoned PR). Citekeys are ``[a-z0-9_-]`` ⇒ valid branch names.
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
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Protocol

from zotero_rdf import from_bibtex, parse_zotero_rdf

from . import emit
from .zotero_bridge import to_store_entries

if TYPE_CHECKING:
    from .emit import EmittedPair
    from .zotero_bridge import StoreEntry


# --------------------------------------------------------------------------- #
# The remote, behind a protocol (real impl: GitHub; tests: an in-memory fake)
# --------------------------------------------------------------------------- #
class StorePublisher(Protocol):
    """The narrow slice of remote-repo operations the publish loop needs."""

    def existing_citekeys(self) -> set[str]:
        """Every citekey already in the store (one recursive ``entries/`` listing)."""

    def branch_exists(self, branch: str) -> bool:
        """Whether ``refs/heads/<branch>`` already exists on the remote."""

    def create_entry_pr(
        self, branch: str, pair: EmittedPair, *, title: str, body: str, commit_message: str
    ) -> str:
        """Commit the pair on a new branch off the base and open a PR; return its URL."""


class GitHubStorePublisher:
    """A :class:`StorePublisher` over a real GitHub repo via the Git Data API.

    Holds a ``github.Repository.Repository`` and the base branch; every write is one
    tree → commit → ref → PR sequence with no local clone.
    """

    def __init__(self, repo, base_branch: str = "main") -> None:
        self._repo = repo
        self._base = base_branch

    def _base_sha(self) -> str:
        return self._repo.get_branch(self._base).commit.sha

    def existing_citekeys(self) -> set[str]:
        tree = self._repo.get_git_tree(self._base_sha(), recursive=True)
        return {
            Path(el.path).stem
            for el in tree.tree
            if el.path.startswith("entries/") and el.path.endswith(".bib")
        }

    def branch_exists(self, branch: str) -> bool:
        from github import GithubException

        try:
            self._repo.get_git_ref(f"heads/{branch}")
            return True
        except GithubException as exc:
            if exc.status == 404:
                return False
            raise

    def create_entry_pr(
        self, branch: str, pair: EmittedPair, *, title: str, body: str, commit_message: str
    ) -> str:
        from github import InputGitTreeElement

        base_sha = self._base_sha()
        base_tree = self._repo.get_git_tree(base_sha)
        elements = [
            InputGitTreeElement(
                path=pair.bib_path.as_posix(), mode="100644", type="blob", content=pair.bib_text
            ),
            InputGitTreeElement(
                path=pair.meta_path.as_posix(), mode="100644", type="blob", content=pair.meta_text
            ),
        ]
        tree = self._repo.create_git_tree(elements, base_tree)
        parent = self._repo.get_git_commit(base_sha)
        commit = self._repo.create_git_commit(commit_message, tree, [parent])
        self._repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
        pr = self._repo.create_pull(title=title, body=body, head=branch, base=self._base)
        return pr.html_url


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


def build_pr_body(store_entry: StoreEntry, bib_text: str) -> str:
    """The reviewer-facing PR body: paper title, authors, and group associations.

    Title and authors come from the canonical bib; groups come straight from the
    entry's ``custom.groups`` (so a multi-group item still surfaces all its PIs in its
    single PR).
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
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# The per-item loop (pure; drives any StorePublisher)
# --------------------------------------------------------------------------- #
@dataclass
class PublishSummary:
    """Outcome of one publish run, for the closing report."""

    created: list[tuple[str, str]] = field(default_factory=list)  # (citekey, pr_url)
    skipped_in_store: list[str] = field(default_factory=list)
    skipped_branch_exists: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (citekey, message)


def publish_entries(
    publisher: StorePublisher,
    entries: Iterable[StoreEntry],
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> PublishSummary:
    """Open one PR per entry; skip in-store citekeys and existing branches.

    The store's citekey set is fetched once (a single recursive ``entries/`` listing).
    Per entry: emit the canonical pair, skip if its citekey is in the store or its
    branch already exists, else commit + open the PR (unless ``dry_run``). ``limit``
    caps the number of PRs *created* in this run — skips don't count against it, so a
    re-run continues through the remaining delta. Errors on one entry are recorded and
    the run continues.
    """
    summary = PublishSummary()
    in_store = publisher.existing_citekeys()

    for store_entry in entries:
        citekey = store_entry.citekey
        if limit is not None and len(summary.created) >= limit:
            break
        try:
            pair = emit.emit_pair(store_entry)
            if citekey in in_store:
                summary.skipped_in_store.append(citekey)
                continue
            if publisher.branch_exists(citekey):
                summary.skipped_branch_exists.append(citekey)
                continue
            if dry_run:
                summary.created.append((citekey, "(dry-run)"))
                continue
            url = publisher.create_entry_pr(
                citekey,
                pair,
                title=citekey,
                body=build_pr_body(store_entry, pair.bib_text),
                commit_message=f"feat: add {citekey}",
            )
            summary.created.append((citekey, url))
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
    ap.add_argument("--limit", type=int, default=None, help="cap PRs created this run")
    ap.add_argument("--dry-run", action="store_true", help="compute + log, no commit/PR")
    ap.add_argument("--token", default=None, help="plain token (else GITHUB_TOKEN; App env vars win)")
    args = ap.parse_args()

    items, cols = parse_zotero_rdf(str(args.rdf))
    entries = to_store_entries(items, cols)
    print(f"source: {len(items)} items → {len(entries)} store entries")

    publisher = make_publisher(
        args.repo,
        base_branch=args.base,
        token=args.token or os.environ.get("GITHUB_TOKEN"),
        app_id=os.environ.get("PUBBOT_APP_ID"),
        private_key=os.environ.get("PUBBOT_PRIVATE_KEY"),
        installation_id=os.environ.get("PUBBOT_INSTALLATION_ID"),
    )

    summary = publish_entries(publisher, entries, limit=args.limit, dry_run=args.dry_run)

    verb = "would create" if args.dry_run else "created"
    print(
        f"{verb} {len(summary.created)} PR(s); "
        f"skipped {len(summary.skipped_in_store)} (in store), "
        f"{len(summary.skipped_branch_exists)} (branch exists); "
        f"{len(summary.errors)} error(s)"
    )
    for citekey, url in summary.created:
        print(f"  + {citekey}  {url}")
    for citekey, message in summary.errors:
        print(f"  ::error:: {citekey}: {message}")
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
