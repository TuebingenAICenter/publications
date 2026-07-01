"""Tests for the blacklist builder (``publication_store.blacklist``).

Three network-free layers:

- **pure core** (:func:`collect_blacklist`) against an in-memory :class:`FakeBlacklister`
  — the union, the ``--since-year`` prune, per-bib error isolation, per-source counts;
- **PR filtering** (:meth:`GitHubBlacklister._pr_bibs`) against a duck-typed fake repo —
  op-kind scope, the merged-PR exclusion, the removed-file skip (no PyGithub, no network);
- **git-history deletion** (:meth:`GitHubBlacklister.deleted_bibs` / ``curated_bibs``)
  against a real throwaway git repo on disk — the delete-vs-re-add distinction for real.

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


# --------------------------------------------------------------------------- #
# PR filtering (GitHubBlacklister._pr_bibs) — duck-typed fake repo, no PyGithub
# --------------------------------------------------------------------------- #
class _FakeLabel:
    def __init__(self, name):
        self.name = name


class _FakeFile:
    def __init__(self, filename, status="added"):
        self.filename = filename
        self.status = status


class _FakeContent:
    def __init__(self, text):
        self.decoded_content = text.encode("utf-8")


class _FakeHead:
    def __init__(self, sha):
        self.sha = sha


class _FakePR:
    def __init__(self, sha, labels, files, *, merged_at=None):
        self.head = _FakeHead(sha)
        self.labels = [_FakeLabel(n) for n in labels]
        self.merged_at = merged_at
        self._files = files

    def get_files(self):
        return self._files


class _FakeRepo:
    """Enough of a PyGithub ``Repository`` for ``_pr_bibs``: pulls + per-sha contents."""

    def __init__(self, pulls_by_state, contents):
        self._pulls = pulls_by_state  # {state: [PR]}
        self._contents = contents  # {(sha, path): text}

    def get_pulls(self, state):
        return self._pulls.get(state, [])

    def get_contents(self, path, ref):
        return _FakeContent(self._contents[(ref, path)])


def _blister(repo):
    return GitHubBlacklister(repo, repo_name="o/r", token="t")


def test_pr_scan_scopes_to_op_labels_and_skips_merged_and_removed():
    open_pr = _FakePR("sha-open", ["new"], [_FakeFile("entries/2024/a.bib")])
    wrong_label = _FakePR("sha-doc", ["documentation"], [_FakeFile("entries/2024/z.bib")])
    closed_new = _FakePR("sha-closed", ["new"], [_FakeFile("entries/2024/b.bib")])
    closed_merged = _FakePR(
        "sha-merged", ["new"], [_FakeFile("entries/2024/c.bib")], merged_at="2024-01-01"
    )
    closed_update = _FakePR("sha-upd", ["update"], [_FakeFile("entries/2024/d.bib")])
    # A closed `new` PR that also removes a file — the removed one must be skipped.
    closed_with_removal = _FakePR(
        "sha-rm",
        ["new"],
        [_FakeFile("entries/2024/e.bib"), _FakeFile("entries/2024/gone.bib", status="removed")],
    )
    contents = {
        ("sha-open", "entries/2024/a.bib"): _bib("a", title="A"),
        ("sha-closed", "entries/2024/b.bib"): _bib("b", title="B"),
        ("sha-rm", "entries/2024/e.bib"): _bib("e", title="E"),
    }
    repo = _FakeRepo(
        {
            "open": [open_pr, wrong_label],
            "closed": [closed_new, closed_merged, closed_update, closed_with_removal],
        },
        contents,
    )
    bl = _blister(repo)

    assert bl.open_pr_bibs() == [_bib("a", title="A")]  # doc PR excluded
    closed = bl.closed_pr_bibs()
    assert _bib("b", title="B") in closed  # rejected `new`
    assert _bib("e", title="E") in closed  # `new` with a removal
    assert len(closed) == 2  # merged + `update` PRs excluded; removed file skipped


# --------------------------------------------------------------------------- #
# git-history deletion (GitHubBlacklister.deleted_bibs / curated_bibs) — real repo
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


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
