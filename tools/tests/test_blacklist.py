"""Tests for the blacklist builder (``publication_store.blacklist``).

Three network-free layers:

- **pure core** (:func:`collect_blacklist`) against an in-memory :class:`FakeBlacklister`
  — the union, the ``--since-year`` prune, per-bib error isolation, per-source counts;
- **PR metadata pass** (:meth:`GitHubBlacklister._pr_meta`) against a duck-typed fake repo
  — op-kind scope + the merged-PR exclusion, now reading SHAs off the listing (no PyGithub,
  no per-PR call);
- **PR content pass** (:meth:`GitHubBlacklister._pr_diff_bibs`) + **git-history deletion**
  (``deleted_bibs`` / ``curated_bibs``) against a real throwaway git repo on disk — the
  diff-vs-base read (guarding the whole-tree footgun), the delete-vs-re-add distinction,
  and the per-PR resilience path, all for real.

Plus a round-trip check that the emitted RDF re-parses. Manual harness, not wired into CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from publication_store import blacklist
from publication_store.blacklist import GitHubBlacklister, collect_blacklist


def _bib(citekey: str, *, title: str, year: str | None = "2024") -> str:
    year_line = f"  year = {{{year}}},\n" if year else ""
    return (
        f"@article{{{citekey},\n"
        f"  title = {{{title}}},\n"
        f"  author = {{Doe, Jane}},\n"
        f"{year_line}"
        f"}}\n"
    )


class FakeBlacklister:
    """In-memory :class:`blacklist.Blacklister`: canned bib strings per source."""

    def __init__(self, *, open_prs=(), closed_prs=(), deleted=(), curated=()):
        self._open = list(open_prs)
        self._closed = list(closed_prs)
        self._deleted = list(deleted)
        self._curated = list(curated)

    def open_pr_bibs(self):
        return list(self._open)

    def closed_pr_bibs(self):
        return list(self._closed)

    def deleted_bibs(self):
        return list(self._deleted)

    def curated_bibs(self):
        return list(self._curated)


# --------------------------------------------------------------------------- #
# Pure core: union + per-source counts
# --------------------------------------------------------------------------- #
def test_all_four_sources_land_with_per_source_counts():
    src = FakeBlacklister(
        open_prs=[_bib("open1", title="Open One")],
        closed_prs=[_bib("closed1", title="Closed One"), _bib("closed2", title="Closed Two")],
        deleted=[_bib("del1", title="Deleted One")],
        curated=[_bib("cur1", title="Curated One")],
    )
    items, summary = collect_blacklist(src)

    assert {it.title for it in items} == {
        "Open One", "Closed One", "Closed Two", "Deleted One", "Curated One"
    }
    assert (summary.open_prs, summary.closed_prs, summary.deletions, summary.curated) == (1, 2, 1, 1)
    assert summary.total() == 5
    assert summary.errors == []


def test_each_item_stamped_with_its_blacklist_reason():
    src = FakeBlacklister(
        open_prs=[_bib("o", title="Open")],
        closed_prs=[_bib("c", title="Closed")],
        deleted=[_bib("d", title="Deleted")],
        curated=[_bib("cur", title="Curated")],
    )
    items, _ = collect_blacklist(src)
    reason = {it.title: it.extra for it in items}
    assert reason == {
        "Open": "Blacklist Reason: open new-publication PR",
        "Closed": "Blacklist Reason: closed (rejected) new-publication PR",
        "Deleted": "Blacklist Reason: deleted from the store",
        "Curated": "Blacklist Reason: hand-curated blacklist entry",
    }


def test_reason_stamp_preserves_existing_extra():
    # A store bib that already carries an `extra` line: the reason appends, not overwrites.
    bib = (
        "@article{withextra,\n"
        "  title = {Has Extra},\n"
        "  author = {Doe, Jane},\n"
        "  note = {some note},\n"
        "}\n"
    )
    items, _ = collect_blacklist(FakeBlacklister(curated=[bib]))
    extra = items[0].extra or ""
    assert extra.endswith("Blacklist Reason: hand-curated blacklist entry")
    assert extra.count("Blacklist Reason:") == 1
    assert extra != "Blacklist Reason: hand-curated blacklist entry"  # prior content kept


def test_multi_source_duplicate_is_kept_no_self_dedup():
    # The same paper via a closed PR and a deletion: both retained (dedup happens
    # downstream in new_publications; the blacklist itself does not collapse).
    src = FakeBlacklister(
        closed_prs=[_bib("dupe", title="Same Paper")],
        deleted=[_bib("dupe", title="Same Paper")],
    )
    items, summary = collect_blacklist(src)
    assert len(items) == 2
    assert summary.total() == 2


# --------------------------------------------------------------------------- #
# --since-year prune
# --------------------------------------------------------------------------- #
def test_since_year_prunes_old_keeps_recent_and_undated():
    src = FakeBlacklister(
        curated=[
            _bib("old", title="Old", year="2019"),
            _bib("recent", title="Recent", year="2024"),
            _bib("undated", title="Undated", year=None),
        ]
    )
    items, summary = collect_blacklist(src, since_year=2022)

    titles = {it.title for it in items}
    assert titles == {"Recent", "Undated"}  # old dropped, undated kept
    assert summary.curated == 2


# --------------------------------------------------------------------------- #
# error isolation
# --------------------------------------------------------------------------- #
def test_one_unparseable_bib_recorded_others_still_emitted():
    src = FakeBlacklister(
        curated=[_bib("good", title="Good"), "@article{broken, title = {oops"],
    )
    items, summary = collect_blacklist(src)

    assert {it.title for it in items} == {"Good"}
    assert len(summary.errors) == 1
    assert summary.errors[0][0] == "curated"


# --------------------------------------------------------------------------- #
# RDF round-trip
# --------------------------------------------------------------------------- #
def test_emitted_rdf_round_trips(tmp_path):
    from zotero_rdf import export_to_rdf, parse_zotero_rdf

    src = FakeBlacklister(open_prs=[_bib("rt", title="Round Trip Paper")])
    items, _ = collect_blacklist(src)
    out = tmp_path / "blacklist.rdf"
    export_to_rdf(items, str(out))

    reparsed, _ = parse_zotero_rdf(str(out))
    assert [it.title for it in reparsed] == ["Round Trip Paper"]
    # The Blacklist Reason stamp survives export → reparse (it's a real RDF field, not just
    # an in-memory annotation), so a reviewer sees provenance in the emitted file.
    assert reparsed[0].extra == "Blacklist Reason: open new-publication PR"


# --------------------------------------------------------------------------- #
# PR metadata pass (GitHubBlacklister._pr_meta) — duck-typed fake repo, no PyGithub
# --------------------------------------------------------------------------- #
class _FakeLabel:
    def __init__(self, name):
        self.name = name


class _FakeRef:
    def __init__(self, sha):
        self.sha = sha


class _FakePR:
    """A PR as the ``get_pulls`` listing exposes it: number, labels, merged_at, and both
    SHAs inline (``head``/``base``) — no ``get_files``/``get_contents`` (content is local)."""

    def __init__(self, number, head_sha, base_sha, labels, *, merged_at=None):
        self.number = number
        self.head = _FakeRef(head_sha)
        self.base = _FakeRef(base_sha)
        self.labels = [_FakeLabel(n) for n in labels]
        self.merged_at = merged_at


class _FakeRepo:
    """Enough of a PyGithub ``Repository`` for the metadata pass: just ``get_pulls``."""

    def __init__(self, pulls_by_state):
        self._pulls = pulls_by_state  # {state: [PR]}

    def get_pulls(self, state):
        return self._pulls.get(state, [])


def _blister(repo):
    return GitHubBlacklister(repo, repo_name="o/r", token="t")


def test_pr_meta_scopes_to_op_labels_and_skips_merged():
    open_new = _FakePR(1, "sha-open", "base-open", ["new"])
    wrong_label = _FakePR(2, "sha-doc", "base-doc", ["documentation"])
    closed_new = _FakePR(3, "sha-closed", "base-closed", ["new"])
    closed_merged = _FakePR(4, "sha-merged", "base-merged", ["new"], merged_at="2024-01-01")
    closed_update = _FakePR(5, "sha-upd", "base-upd", ["update"])
    repo = _FakeRepo(
        {
            "open": [open_new, wrong_label],
            "closed": [closed_new, closed_merged, closed_update],
        }
    )
    bl = _blister(repo)

    # open: only the `new`-labelled PR, with both SHAs read off the listing.
    assert bl._pr_meta("open", unmerged_only=False) == [(1, "sha-open", "base-open")]
    # closed: `new` + unmerged only — merged `new` and the `update` PR both excluded.
    assert bl._pr_meta("closed", unmerged_only=True) == [(3, "sha-closed", "base-closed")]


# --------------------------------------------------------------------------- #
# git-history deletion (GitHubBlacklister.deleted_bibs / curated_bibs) — real repo
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_file(repo: Path, rel: str, text: str, msg: str):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", msg)


@pytest.fixture
def store_repo(tmp_path):
    repo = tmp_path / "store"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    return repo


def test_deleted_bibs_returns_gone_but_not_readded(store_repo):
    # gone.bib: added then deleted, absent at HEAD → blacklisted.
    _commit_file(store_repo, "entries/2024/gone.bib", _bib("gone", title="Gone"), "add gone")
    _git(store_repo, "rm", "-q", "entries/2024/gone.bib")
    _git(store_repo, "commit", "-m", "delete gone")

    # readd.bib: added, deleted, re-added → present at HEAD → NOT blacklisted.
    _commit_file(store_repo, "entries/2024/readd.bib", _bib("readd", title="V1"), "add readd")
    _git(store_repo, "rm", "-q", "entries/2024/readd.bib")
    _git(store_repo, "commit", "-m", "delete readd")
    _commit_file(store_repo, "entries/2024/readd.bib", _bib("readd", title="V2"), "re-add readd")

    # kept.bib: only ever added → present at HEAD → NOT blacklisted.
    _commit_file(store_repo, "entries/2024/kept.bib", _bib("kept", title="Kept"), "add kept")

    bl = _blister(repo=None)
    bl._clone_dir = store_repo  # skip the network clone; drive the real git logic

    deleted = bl.deleted_bibs()
    assert deleted == [_bib("gone", title="Gone")]


# --------------------------------------------------------------------------- #
# PR content pass (GitHubBlacklister._pr_diff_bibs / _pr_bibs) — real repo, no network
# --------------------------------------------------------------------------- #
def test_pr_diff_reads_only_added_and_modified_not_the_whole_tree(store_repo):
    # Base tree: an entry the PR leaves alone, one it will modify, one it will delete.
    _commit_file(store_repo, "entries/2024/untouched.bib", _bib("untouched", title="Untouched"), "base")
    _commit_file(store_repo, "entries/2024/existing.bib", _bib("existing", title="Existing V1"), "base")
    _commit_file(store_repo, "entries/2024/todelete.bib", _bib("todelete", title="To Delete"), "base")
    base_sha = _rev(store_repo, "HEAD")

    # A "PR head": add one entry, modify one, delete one — off the same base.
    _commit_file(store_repo, "entries/2024/new.bib", _bib("new", title="New"), "pr: add new")
    _commit_file(store_repo, "entries/2024/existing.bib", _bib("existing", title="Existing V2"), "pr: modify")
    _git(store_repo, "rm", "-q", "entries/2024/todelete.bib")
    _git(store_repo, "commit", "-m", "pr: delete")
    head_sha = _rev(store_repo, "HEAD")

    bl = _blister(repo=None)
    bl._clone_dir = store_repo  # skip the network clone; drive the real git logic

    bibs = bl._pr_diff_bibs(head_sha, base_sha)
    titles = {b.split("title = {")[1].split("}")[0] for b in bibs}
    # Added + modified-at-head only. The untouched entry (the whole-tree footgun) and the
    # deletion are both absent. Because the deleted + added bibs are structurally similar,
    # this also guards the `--no-renames` fix: without it git folds the pair into a rename
    # and drops the addition ("New") entirely.
    assert titles == {"New", "Existing V2"}


def test_pr_bibs_resilience_skips_unreadable_pr_records_error(store_repo):
    # One good PR (readable head + base) and one whose base SHA is orphaned.
    _commit_file(store_repo, "entries/2024/base.bib", _bib("base", title="Base"), "base")
    base_sha = _rev(store_repo, "HEAD")
    _commit_file(store_repo, "entries/2024/good.bib", _bib("good", title="Good"), "pr: add good")
    good_head = _rev(store_repo, "HEAD")

    good = _FakePR(1, good_head, base_sha, ["new"])
    orphaned_base = _FakePR(2, good_head, "0" * 40, ["new"])  # base unreachable → skip+record
    bl = _blister(_FakeRepo({"open": [good, orphaned_base]}))
    bl._clone_dir = store_repo
    bl._fetch_pr_heads = lambda numbers, *, source: set(numbers)  # heads already local

    bibs = bl.open_pr_bibs()
    assert bibs == [_bib("good", title="Good")]  # the good PR still lands
    assert len(bl.errors) == 1 and bl.errors[0][0] == "open_prs" and "#2" in bl.errors[0][1]


def test_source_errors_fold_into_collect_blacklist_summary(store_repo):
    _commit_file(store_repo, "entries/2024/base.bib", _bib("base", title="Base"), "base")
    base_sha = _rev(store_repo, "HEAD")
    _commit_file(store_repo, "entries/2024/good.bib", _bib("good", title="Good"), "pr: add good")
    good_head = _rev(store_repo, "HEAD")

    good = _FakePR(1, good_head, base_sha, ["new"])
    orphaned = _FakePR(2, good_head, "0" * 40, ["new"])
    bl = _blister(_FakeRepo({"open": [good, orphaned]}))
    bl._clone_dir = store_repo
    bl._fetch_pr_heads = lambda numbers, *, source: set(numbers)

    items, summary = collect_blacklist(bl)
    assert {it.title for it in items} == {"Good"}
    assert summary.open_prs == 1
    # The unreadable PR surfaces as a run error, not a crash.
    assert len(summary.errors) == 1 and summary.errors[0][0] == "open_prs"


def test_curated_bibs_reads_in_repo_glob_and_includes(store_repo, tmp_path):
    _commit_file(store_repo, "blacklist/curated.bib", _bib("cur", title="Curated"), "add curated")
    extra = tmp_path / "local.bib"
    extra.write_text(_bib("local", title="Local Extra"), encoding="utf-8")

    bl = GitHubBlacklister(repo=None, repo_name="o/r", token="t", include=[str(extra)])
    bl._clone_dir = store_repo

    curated = bl.curated_bibs()
    assert _bib("cur", title="Curated") in curated
    assert _bib("local", title="Local Extra") in curated
    assert len(curated) == 2
