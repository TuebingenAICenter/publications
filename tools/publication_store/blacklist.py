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
the test suite. Unlike them, the real impl also ``git clone``\\ s the store: deletion
detection needs *history*, which the no-clone API path deliberately avoids. That is a
once-a-month cost, so it shells out to the ``git`` binary — no new Python dependency.

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
    """A :class:`Blacklister` over a real store repo: GitHub API for the PR scans, a
    once-per-run ``git clone`` for the deletion + curated scans.

    Holds a ``github.Repository.Repository`` (PR scans), plus the ``OWNER/REPO`` name and
    a bearer token used to clone over HTTPS. The clone is lazy and shared between
    :meth:`deleted_bibs` and :meth:`curated_bibs`.
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

    # -- PR scans (API, no clone) -------------------------------------------- #
    def _pr_bibs(self, state: str, *, unmerged_only: bool) -> list[str]:
        """``.bib`` text added by every op-kind PR in ``state``, read at ``pr.head.sha``.

        ``refs/pull/N/head`` persists after branch deletion, so rejected/swept PRs stay
        readable. Files the PR *removes* are skipped (they'd 404 at head); only ``entries/
        **/*.bib`` blobs are read.
        """
        out: list[str] = []
        for pr in self._repo.get_pulls(state=state):
            labels = {label.name for label in pr.labels}
            if not (labels & self._op_labels):
                continue
            if unmerged_only and pr.merged_at is not None:
                continue
            for f in pr.get_files():
                if f.status == "removed":
                    continue
                if f.filename.startswith("entries/") and f.filename.endswith(".bib"):
                    blob = self._repo.get_contents(f.filename, ref=pr.head.sha)
                    out.append(blob.decoded_content.decode("utf-8"))
        return out

    def open_pr_bibs(self) -> list[str]:
        return self._pr_bibs("open", unmerged_only=False)

    def closed_pr_bibs(self) -> list[str]:
        return self._pr_bibs("closed", unmerged_only=True)

    # -- clone-backed scans -------------------------------------------------- #
    def _clone(self) -> Path:
        """Blobless clone of the store (full commit graph, blobs fetched on demand).

        Shallow would drop the deletion history we need; ``--filter=blob:none`` keeps the
        graph while deferring blob download to the ``git show`` reads.
        """
        if self._clone_dir is None:
            dest = Path(tempfile.mkdtemp(prefix="pubstore-blacklist-"))
            url = f"https://x-access-token:{self._token}@github.com/{self._repo_name}.git"
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--quiet", url, str(dest)],
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
    ``summary.errors`` (tagged with its source) and skipped — it never sinks the run. An
    optional ``since_year`` prunes items older than that publication year (undated kept).
    Order is source-by-source, items in source order; no self-dedup (see module docstring).
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
                    items.append(item)
                    count += 1
        setattr(summary, source, count)

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
