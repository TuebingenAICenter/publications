"""Tests for the PR-expiry sweep loop (``publication_store.sweep``).

Drives :func:`sweep_prs` against an in-memory :class:`FakeReviewer` — no network, no
PyGithub — to pin the behaviours that make the sweep safe to run on a cron:

- **accept never forces a merge** — only cleanly-mergeable expired PRs merge; the rest are
  reported and left open;
- **reject** closes, comments, and deletes the head branch;
- **age gate** — not-yet-expired PRs are untouched;
- **scope** — non-publication PRs (no op-kind label) are never seen;
- **``--limit``** caps PRs *acted on*, not seen; **``--dry-run``** changes nothing;
- one failing PR doesn't sink the run.

Manual developer harness, **not** wired into CI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from publication_store import sweep
from publication_store.sweep import OpenPR

NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


def _pr(number, *, age_days, mergeable_state="clean", labels=("new",)):
    """An :class:`OpenPR` created ``age_days`` before :data:`NOW`."""
    return OpenPR(
        number=number,
        title=f"New Publication: paper {number}",
        html_url=f"https://example.test/pr/{number}",
        head_branch=f"branch{number}",
        created_at=NOW - timedelta(days=age_days),
        labels=set(labels),
        mergeable_state=mergeable_state,
    )


class FakeReviewer:
    """In-memory :class:`sweep.StoreReviewer`: records merges, closes, branch-deletes."""

    def __init__(self, prs, *, op_labels=sweep.DEFAULT_OP_LABELS, fail_on=()):
        # Mirror the real reviewer's scoping: only publication PRs are ever surfaced.
        self._prs = [pr for pr in prs if pr.labels & set(op_labels)]
        self._fail_on = set(fail_on)
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.deleted_branches: list[str] = []
        self.comments: dict[int, str] = {}

    def publication_prs(self):
        return list(self._prs)

    def merge_pr(self, number, head_branch):
        if number in self._fail_on:
            raise RuntimeError("boom")
        self.merged.append(number)
        self.deleted_branches.append(head_branch)

    def close_pr(self, number, head_branch, *, comment):
        if number in self._fail_on:
            raise RuntimeError("boom")
        self.closed.append(number)
        self.comments[number] = comment
        self.deleted_branches.append(head_branch)


def _sweep(reviewer, *, on_expiry, older_than=timedelta(days=30), **kw):
    return sweep.sweep_prs(reviewer, on_expiry=on_expiry, older_than=older_than, now=NOW, **kw)


# --------------------------------------------------------------------------- #
# accept
# --------------------------------------------------------------------------- #
def test_accept_merges_expired_clean_pr_and_deletes_branch():
    rev = FakeReviewer([_pr(1, age_days=40)])
    summary = _sweep(rev, on_expiry="accept")

    assert [pr.number for pr in summary.merged] == [1]
    assert rev.merged == [1] and rev.deleted_branches == ["branch1"]
    assert summary.skipped_not_mergeable == [] and rev.closed == []


@pytest.mark.parametrize("state", ["dirty", "blocked", "unstable", "behind", "unknown", None])
def test_accept_skips_expired_non_clean_pr_untouched(state):
    rev = FakeReviewer([_pr(2, age_days=40, mergeable_state=state)])
    summary = _sweep(rev, on_expiry="accept")

    assert summary.merged == []
    assert len(summary.skipped_not_mergeable) == 1
    label, reason = summary.skipped_not_mergeable[0]
    assert "#2" in label and reason  # a human-readable reason was recorded
    assert rev.merged == [] and rev.deleted_branches == []  # PR left fully untouched


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #
def test_reject_closes_comments_and_deletes_branch():
    rev = FakeReviewer([_pr(3, age_days=40, mergeable_state="dirty")])  # mergeability irrelevant
    summary = _sweep(rev, on_expiry="reject")

    assert [pr.number for pr in summary.closed] == [3]
    assert rev.closed == [3] and rev.deleted_branches == ["branch3"]
    assert "Auto-closed" in rev.comments[3] and "30 days" in rev.comments[3]
    assert rev.merged == []


# --------------------------------------------------------------------------- #
# age gate + scope
# --------------------------------------------------------------------------- #
def test_not_yet_expired_pr_is_untouched():
    rev = FakeReviewer([_pr(4, age_days=10)])  # younger than the 30d threshold
    summary = _sweep(rev, on_expiry="accept")

    assert summary.expired == [] and summary.skipped_not_expired == 1
    assert rev.merged == [] and rev.closed == []


def test_threshold_boundary_is_inclusive():
    rev = FakeReviewer([_pr(5, age_days=30)])  # exactly at the threshold ⇒ expired
    summary = _sweep(rev, on_expiry="accept")
    assert [pr.number for pr in summary.merged] == [5]


def test_non_publication_pr_is_never_seen():
    # A PR carrying no op-kind label is filtered out by the reviewer, not the loop.
    rev = FakeReviewer([_pr(6, age_days=99, labels={"documentation"})])
    summary = _sweep(rev, on_expiry="reject")

    assert summary.expired == [] and summary.skipped_not_expired == 0
    assert rev.closed == []


# --------------------------------------------------------------------------- #
# limit / dry-run / resilience
# --------------------------------------------------------------------------- #
def test_limit_caps_prs_acted_on_not_seen():
    # One not-yet-expired PR (must not consume budget) then three expired, limit=2.
    rev = FakeReviewer(
        [_pr(1, age_days=5), _pr(2, age_days=40), _pr(3, age_days=41), _pr(4, age_days=42)]
    )
    summary = _sweep(rev, on_expiry="accept", limit=2)

    assert summary.skipped_not_expired == 1
    assert len(summary.expired) == 3  # all three expired ones are recorded as seen
    assert rev.merged == [2, 3]  # but only two are acted on


def test_dry_run_records_actions_but_changes_nothing():
    rev = FakeReviewer([_pr(1, age_days=40), _pr(2, age_days=41)])
    summary = _sweep(rev, on_expiry="accept", dry_run=True)

    assert [pr.number for pr in summary.merged] == [1, 2]  # intended actions recorded
    assert rev.merged == [] and rev.deleted_branches == []  # nothing actually done


def test_dry_run_reject_changes_nothing():
    rev = FakeReviewer([_pr(1, age_days=40)])
    summary = _sweep(rev, on_expiry="reject", dry_run=True)

    assert [pr.number for pr in summary.closed] == [1]
    assert rev.closed == [] and rev.comments == {}


def test_one_failing_pr_does_not_sink_the_run():
    rev = FakeReviewer([_pr(1, age_days=40), _pr(2, age_days=41)], fail_on={1})
    summary = _sweep(rev, on_expiry="accept")

    assert [label for label, _ in summary.errors][0].startswith("#1")
    assert [pr.number for pr in summary.merged] == [2]  # the other PR still merged


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_parse_age_units():
    assert sweep._parse_age("30d") == timedelta(days=30)
    assert sweep._parse_age("2w") == timedelta(weeks=2)
    assert sweep._parse_age("12h") == timedelta(hours=12)
    assert sweep._parse_age("7") == timedelta(days=7)  # bare int ⇒ days
    assert sweep._parse_age("0d") == timedelta(0)
    with pytest.raises(ValueError):
        sweep._parse_age("-1d")
    with pytest.raises(ValueError):
        sweep._parse_age("soon")


def test_mergeable_maps_states_to_reasons():
    assert sweep._mergeable(_pr(1, age_days=1, mergeable_state="clean")) == (True, "")
    ok, reason = sweep._mergeable(_pr(1, age_days=1, mergeable_state="dirty"))
    assert ok is False and reason == "merge conflict"
    ok, reason = sweep._mergeable(_pr(1, age_days=1, mergeable_state="behind"))
    assert ok is False and reason == "behind base"
    ok, reason = sweep._mergeable(_pr(1, age_days=1, mergeable_state=None))
    assert ok is False and "not computed" in reason
