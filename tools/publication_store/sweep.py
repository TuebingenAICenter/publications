"""``pubstore-sweep`` — act on publication PRs left open past a threshold.

``pubstore-publish`` opens one PR per publication against the store repo; the monthly
cron can leave 200+ of them open across several reviewers. This companion CLI sweeps the
backlog: for publication PRs older than ``--older-than`` it either **auto-accepts**
(squash-merges the stale-but-green ones) or **auto-rejects** (closes + comments + deletes
the head branch), per a required ``--on-expiry`` policy. ``--dry-run`` reports what each
expired PR *would* get without touching anything — the fetch/list mode.

Like ``pubstore-publish`` it is API-only (no clone), mints the same App-installation auth
(:func:`publication_store._github.github_repo`), and puts the remote behind a
:class:`StoreReviewer` protocol so the per-PR loop (:func:`sweep_prs`) is testable with an
in-memory fake — no network in the test suite.

Design (see ``planning/tasks`` for the rationale):

- **Scope = publication PRs only.** Only open PRs carrying a ``pubstore-publish`` op-kind
  label (``new`` / ``update`` / ``rename``) are considered; unrelated PRs are never seen.
- **Accept never forces a merge.** On ``accept`` a PR that is expired but not *cleanly*
  mergeable (failing checks, conflict, behind base, or mergeability not yet computed) is
  skipped and reported, left open for a human. Only ``clean`` PRs auto-merge.
- **Reject** comments the timeout reason, closes the PR, and deletes the head branch (so a
  later re-publish of the same citekey re-opens cleanly past publish's branch-exists skip).
- **Age basis is ``created_at``** — the PR has existed past the threshold, independent of
  reviewer activity.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

#: The ``pubstore-publish`` op-kind labels that mark a PR as a publication PR. A PR is in
#: scope iff its label set intersects this (overridable via ``--label``).
DEFAULT_OP_LABELS = ("new", "update", "rename")

#: Default expiry threshold — matches the monthly publish cadence.
DEFAULT_OLDER_THAN = "30d"


# --------------------------------------------------------------------------- #
# The remote, behind a protocol (real impl: GitHub; tests: an in-memory fake)
# --------------------------------------------------------------------------- #
@dataclass
class OpenPR:
    """The slice of an open PR the sweep loop reasons about."""

    number: int
    title: str
    html_url: str
    head_branch: str
    created_at: datetime  # tz-aware
    labels: set[str]
    mergeable_state: str | None  # GitHub's computed state ("clean", "dirty", ...)


class StoreReviewer(Protocol):
    """The narrow slice of remote-repo operations the sweep loop needs."""

    def publication_prs(self) -> list[OpenPR]:
        """Every open publication PR (filtered to the op-kind labels)."""

    def merge_pr(self, number: int, head_branch: str) -> None:
        """Squash-merge PR ``number``, then delete its head branch."""

    def close_pr(self, number: int, head_branch: str, *, comment: str) -> None:
        """Leave ``comment``, close PR ``number``, then delete its head branch."""


class GitHubStoreReviewer:
    """A :class:`StoreReviewer` over a real GitHub repo via PyGithub.

    Holds a ``github.Repository.Repository`` and the op-kind label set that scopes the
    listing to publication PRs.
    """

    def __init__(self, repo, *, op_labels: tuple[str, ...] = DEFAULT_OP_LABELS) -> None:
        self._repo = repo
        self._op_labels = set(op_labels)

    def publication_prs(self) -> list[OpenPR]:
        out: list[OpenPR] = []
        for pr in self._repo.get_pulls(state="open"):
            labels = {label.name for label in pr.labels}
            if not (labels & self._op_labels):
                continue
            # GitHub computes mergeability asynchronously, so a freshly listed PR can
            # report None; re-fetch the PR once to give it a chance to settle.
            state = pr.mergeable_state
            if state is None:
                state = self._repo.get_pull(pr.number).mergeable_state
            out.append(
                OpenPR(
                    number=pr.number,
                    title=pr.title,
                    html_url=pr.html_url,
                    head_branch=pr.head.ref,
                    created_at=pr.created_at,
                    labels=labels,
                    mergeable_state=state,
                )
            )
        return out

    def _delete_branch(self, head_branch: str) -> None:
        self._repo.get_git_ref(f"heads/{head_branch}").delete()

    def merge_pr(self, number: int, head_branch: str) -> None:
        self._repo.get_pull(number).merge(merge_method="squash")
        self._delete_branch(head_branch)

    def close_pr(self, number: int, head_branch: str, *, comment: str) -> None:
        pr = self._repo.get_pull(number)
        pr.create_issue_comment(comment)
        pr.edit(state="closed")
        self._delete_branch(head_branch)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_age(text: str) -> timedelta:
    """A ``timedelta`` from ``30d`` / ``2w`` / ``12h`` / a bare int (days).

    Raises ``ValueError`` on anything else (caller surfaces it as an arg error).
    """
    text = text.strip().lower()
    units = {"h": "hours", "d": "days", "w": "weeks"}
    if text and text[-1] in units:
        value, kw = text[:-1], units[text[-1]]
    else:
        value, kw = text, "days"  # bare number ⇒ days
    number = int(value)
    if number < 0:
        raise ValueError(f"age must not be negative: {text!r}")
    return timedelta(**{kw: number})


#: Human-readable reason per non-``clean`` ``mergeable_state``, for the skip report.
_MERGE_BLOCKERS = {
    "dirty": "merge conflict",
    "blocked": "checks failing or blocked",
    "unstable": "checks failing or blocked",
    "behind": "behind base",
    "unknown": "mergeability not computed yet",
    None: "mergeability not computed yet",
}


def _mergeable(pr: OpenPR) -> tuple[bool, str]:
    """``(is_clean, reason)`` for ``pr``; ``reason`` is ``""`` when clean."""
    if pr.mergeable_state == "clean":
        return True, ""
    return False, _MERGE_BLOCKERS.get(pr.mergeable_state, f"not mergeable ({pr.mergeable_state})")


def _reject_comment(older_than: timedelta) -> str:
    """The comment left on an auto-rejected PR."""
    return (
        f"⏰ Auto-closed by `pubstore-sweep`: this publication PR was open longer than "
        f"{_format_age(older_than)} without being merged. Re-run `pubstore-publish` to "
        f"re-open it if it should still land."
    )


def _format_age(delta: timedelta) -> str:
    """A compact human rendering of a threshold (``30 days`` / ``12 hours``)."""
    total_hours = delta.days * 24 + delta.seconds // 3600
    if delta.days and not (total_hours % 24):
        return f"{delta.days} day{'s' if delta.days != 1 else ''}"
    return f"{total_hours} hour{'s' if total_hours != 1 else ''}"


# --------------------------------------------------------------------------- #
# The per-PR loop (pure; drives any StoreReviewer)
# --------------------------------------------------------------------------- #
@dataclass
class SweepSummary:
    """Outcome of one sweep run, for the closing report."""

    #: Every expired PR seen (always populated, so dry-run/fetch has the full set).
    expired: list[OpenPR] = field(default_factory=list)
    merged: list[OpenPR] = field(default_factory=list)
    closed: list[OpenPR] = field(default_factory=list)
    #: (pr label, reason) for expired-but-not-cleanly-mergeable PRs left for a human.
    skipped_not_mergeable: list[tuple[str, str]] = field(default_factory=list)
    skipped_not_expired: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (pr label, message)

    def acted(self) -> int:
        """How many PRs this run has merged or closed so far."""
        return len(self.merged) + len(self.closed)


def _pr_label(pr: OpenPR) -> str:
    """A short identifier for reports (``#<number> <branch>``)."""
    return f"#{pr.number} {pr.head_branch}"


def sweep_prs(
    reviewer: StoreReviewer,
    *,
    on_expiry: str,
    older_than: timedelta,
    now: datetime,
    limit: int | None = None,
    dry_run: bool = False,
) -> SweepSummary:
    """Accept or reject publication PRs open longer than ``older_than``.

    Lists the publication PRs once, partitions them on age (``now - created_at >=
    older_than``), then for each expired PR applies ``on_expiry``:

    - ``"accept"`` — squash-merge **only if** the PR is cleanly mergeable; otherwise record
      it in ``skipped_not_mergeable`` and leave it open for a human.
    - ``"reject"`` — comment the timeout reason, close, and delete the head branch.

    ``limit`` caps the number of PRs *acted on* (merged + closed), not PRs seen — so a
    re-run continues through the remaining backlog. ``dry_run`` records the intended action
    but performs no merge/close. An error on one PR is recorded and the run continues.
    """
    summary = SweepSummary()
    for pr in reviewer.publication_prs():
        if now - pr.created_at < older_than:
            summary.skipped_not_expired += 1
            continue
        summary.expired.append(pr)

    for pr in summary.expired:
        if limit is not None and summary.acted() >= limit:
            break
        try:
            if on_expiry == "accept":
                clean, reason = _mergeable(pr)
                if not clean:
                    summary.skipped_not_mergeable.append((_pr_label(pr), reason))
                    continue
                if not dry_run:
                    reviewer.merge_pr(pr.number, pr.head_branch)
                summary.merged.append(pr)
            else:  # reject
                if not dry_run:
                    reviewer.close_pr(
                        pr.number, pr.head_branch, comment=_reject_comment(older_than)
                    )
                summary.closed.append(pr)
        except Exception as exc:  # one bad PR must not sink the whole run
            summary.errors.append((_pr_label(pr), str(exc)))

    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Accept or reject publication PRs left open past a threshold."
    )
    ap.add_argument("--repo", required=True, help="OWNER/REPO of the store repository")
    ap.add_argument(
        "--on-expiry",
        required=True,
        choices=("accept", "reject"),
        help="accept = squash-merge stale-but-green PRs; reject = close + comment + delete branch",
    )
    ap.add_argument(
        "--older-than",
        default=DEFAULT_OLDER_THAN,
        help=f"age threshold: 30d / 2w / 12h / bare int days (default: {DEFAULT_OLDER_THAN})",
    )
    ap.add_argument(
        "--label",
        action="append",
        default=None,
        help="op-kind label that marks a publication PR (repeatable; "
        f"default: {', '.join(DEFAULT_OP_LABELS)})",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="cap PRs acted on this run (merges + closes)"
    )
    ap.add_argument("--dry-run", action="store_true", help="report intended actions, change nothing")
    ap.add_argument("--token", default=None, help="plain token (else GITHUB_TOKEN; App env vars win)")
    args = ap.parse_args()

    try:
        older_than = _parse_age(args.older_than)
    except ValueError:
        ap.error(f"invalid --older-than {args.older_than!r}: use 30d / 2w / 12h / bare int days")

    from ._github import github_repo

    repo = github_repo(
        args.repo,
        token=args.token or os.environ.get("GITHUB_TOKEN"),
        app_id=os.environ.get("PUBBOT_APP_ID"),
        private_key=os.environ.get("PUBBOT_PRIVATE_KEY"),
        installation_id=os.environ.get("PUBBOT_INSTALLATION_ID"),
    )
    op_labels = tuple(args.label) if args.label else DEFAULT_OP_LABELS
    reviewer = GitHubStoreReviewer(repo, op_labels=op_labels)

    summary = sweep_prs(
        reviewer,
        on_expiry=args.on_expiry,
        older_than=older_than,
        now=datetime.now(timezone.utc),
        limit=args.limit,
        dry_run=args.dry_run,
    )

    acted = "would " if args.dry_run else ""
    if args.on_expiry == "accept":
        print(
            f"{len(summary.expired)} expired PR(s); {acted}merged {len(summary.merged)}, "
            f"skipped {len(summary.skipped_not_mergeable)} (not mergeable), "
            f"{summary.skipped_not_expired} not yet expired; {len(summary.errors)} error(s)"
        )
        for pr in summary.merged:
            print(f"  {'~' if args.dry_run else '✓'} {_pr_label(pr)}  {pr.html_url}")
        for label, reason in summary.skipped_not_mergeable:
            print(f"  ::warning:: {label}: left open — {reason}")
    else:
        print(
            f"{len(summary.expired)} expired PR(s); {acted}closed {len(summary.closed)}, "
            f"{summary.skipped_not_expired} not yet expired; {len(summary.errors)} error(s)"
        )
        for pr in summary.closed:
            print(f"  {'~' if args.dry_run else '✗'} {_pr_label(pr)}  {pr.html_url}")

    for label, message in summary.errors:
        print(f"  ::error:: {label}: {message}")
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
