"""Tests for the one-off PR-body backfill (``publication_store.backfill_pr_bodies``).

Drives :func:`backfill_pr_bodies.plan_updates` against an in-memory fake repo — no
network, no PyGithub — pinning the properties that make a body rewrite safe to run by
hand:

- only publisher branches (``<citekey>`` / ``update/<citekey>`` / ``rename/<citekey>``)
  are considered; bot branches like ``codex/…`` are left alone;
- the body is regenerated from the pair committed on the PR **head**, not from the
  store's ``main``, so the reviewer sees the entry the PR actually adds;
- an already-current body is not reported (a re-run is a no-op).

Manual developer harness, **not** wired into CI.
"""

from __future__ import annotations

from publication_store import backfill_pr_bodies as backfill
from publication_store import emit, publish
from publication_store.zotero_bridge import to_store_entries
from zotero_rdf import Creator, JournalArticle, ZoteroCollection


class _Content:
    def __init__(self, text: str) -> None:
        self.decoded_content = text.encode("utf-8")


class _File:
    def __init__(self, filename: str, status: str = "added", previous_filename=None) -> None:
        self.filename = filename
        self.status = status
        self.previous_filename = previous_filename


class _Head:
    def __init__(self, ref: str, sha: str = "headsha") -> None:
        self.ref = ref
        self.sha = sha


class _PR:
    def __init__(self, number: int, branch: str, files: list[_File], body: str = "") -> None:
        self.number = number
        self.head = _Head(branch)
        self.body = body
        self._files = files
        self.edits: list[str] = []

    def get_files(self):
        return self._files

    def edit(self, *, body: str) -> None:
        self.edits.append(body)
        self.body = body


class _Repo:
    """Fake repo: serves ``get_contents`` from a path->text map; holds the PR list."""

    def __init__(self, pulls: list[_PR], files: dict[str, str]) -> None:
        self._pulls = pulls
        self._files = files

    def get_pulls(self, *, state=None, base=None):
        return list(self._pulls)

    def get_pull(self, number):
        for pr in self._pulls:
            if pr.number == number:
                return pr
        raise KeyError(number)

    def get_contents(self, path, ref=None):
        if not self._files:
            raise FileNotFoundError(path)
        # A real repo serves the stored (base-branch) pair at the same path the PR head
        # uses, so the base lookup is namespaced to keep the two distinguishable.
        key = f"base:{path}" if ref == "main" and f"base:{path}" in self._files else path
        if key not in self._files:
            raise FileNotFoundError(path)
        return _Content(self._files[key])


def _entry(title: str, *, url: str | None = None):
    item = JournalArticle(
        title=title, publicationTitle="Nature", date="2025",
        creators=[Creator(creatorType="author", firstName="Jane", lastName="Smith")],
        url=url,
    )
    col = ZoteroCollection(name="bethge")
    col.add(item)
    (se,) = to_store_entries([item], [col])
    return se


def _repo_for(entries, branches, bodies=None):
    """A fake repo holding each entry's committed pair at its canonical path.

    For an ``update/…`` branch the canonical path is *also* seeded as the base-branch
    version (a distinct "before" pair), so the regenerated Update body has a diff.
    """
    files: dict[str, str] = {}
    pulls: list[_PR] = []
    for i, (entry, branch) in enumerate(zip(entries, branches)):
        pair = emit.emit_pair(entry)
        files[pair.bib_path.as_posix()] = pair.bib_text
        files[pair.meta_path.as_posix()] = pair.meta_text
        if branch.startswith("update/"):
            files[f"base:{pair.bib_path.as_posix()}"] = "@misc{before,\n\ttitle = {Before},\n}\n"
            files[f"base:{pair.meta_path.as_posix()}"] = '{\n  "zotero": {},\n  "custom": {}\n}\n'
        path_files = [_File(pair.bib_path.as_posix()), _File(pair.meta_path.as_posix())]
        if branch.startswith("rename/"):
            # A rename PR names the superseded citekey via a removed (or GitHub-reported
            # `renamed`, with previous_filename) pair alongside the new one.
            old_stem = f"{branch.split('/', 1)[1]}_old"
            old_bib = f"entries/2019/{old_stem}.bib"
            if i % 2 == 0:
                path_files.append(_File(old_bib, status="removed"))
            else:
                path_files[0] = _File(
                    pair.bib_path.as_posix(), status="renamed", previous_filename=old_bib
                )
        pulls.append(_PR(i + 1, branch, path_files, body=(bodies or {}).get(branch, "")))
    return _Repo(pulls, files)


def test_plan_updates_skips_bot_branches():
    entry = _entry("Paper", url="https://arxiv.org/abs/2401.12345")
    repo = _repo_for([entry], ["codex/mention-ai-center-foo"])

    assert list(backfill.plan_updates(repo, "main")) == []


def test_plan_updates_reports_a_new_body_carrying_the_pdf_link():
    entry = _entry("Paper", url="https://arxiv.org/abs/2401.12345")
    repo = _repo_for([entry], ["smith_paper_2025"])

    planned = list(backfill.plan_updates(repo, "main"))

    assert len(planned) == 1
    pr, new_body, old_body = planned[0]
    assert old_body == ""  # the pre-change PR body
    assert "**ARXIV PDF:**" in new_body


def test_plan_updates_is_a_noop_when_body_is_already_current():
    entry = _entry("Paper", url="https://arxiv.org/abs/2401.12345")
    repo = _repo_for([entry], ["smith_paper_2025"])
    # Seed the PR body with exactly what the renderer would produce.
    pr = repo.get_pulls()[0]
    loaded = backfill._head_pair(repo, pr)
    assert loaded is not None
    entry_loaded, pair = loaded
    pr.body = publish.build_pr_body(entry_loaded, pair)

    assert list(backfill.plan_updates(repo, "main")) == []


def test_plan_updates_handles_update_and_rename_branches():
    updated = _entry("Updated Paper", url="https://arxiv.org/abs/2401.99999")
    renamed = _entry("Renamed Paper", url="https://arxiv.org/abs/2402.11111")
    repo = _repo_for([updated, renamed], ["update/smith_updated_2025", "rename/smith_renamed_2025"])

    planned = list(backfill.plan_updates(repo, "main"))

    assert len(planned) == 2
    by_branch = {pr.head.ref: body for pr, body, _ in planned}
    # An update body has the diff section; a rename body announces the supersession.
    assert "#### What changed" in by_branch["update/smith_updated_2025"]
    assert "### Rename Publication" in by_branch["rename/smith_renamed_2025"]


def test_rename_target_is_read_from_removed_or_renamed_files():
    # GitHub reports the superseded pair either as `removed` files or as a single
    # `renamed` file carrying previous_filename; both must resolve the target.
    removed = _entry("A", url="https://arxiv.org/abs/2401.11111")
    renamed = _entry("B", url="https://arxiv.org/abs/2402.22222")
    repo = _repo_for([removed, renamed], ["rename/aa_2025", "rename/bb_2025"])

    planned = list(backfill.plan_updates(repo, "main"))

    assert len(planned) == 2  # neither shape is skipped
    assert all("### Rename Publication" in body for _, body, _ in planned)


def test_plan_updates_dedupes_and_honors_limit():
    entries = [_entry(f"Paper {i}", url=f"https://arxiv.org/abs/2401.{i:05d}") for i in range(4)]
    repo = _repo_for(entries, [f"smith_paper_{i}_2025" for i in range(4)])
    repo._pulls.append(repo._pulls[0])  # a PR seen twice (pagination overlap)

    planned = list(backfill.plan_updates(repo, "main"))
    assert len(planned) == 4  # the repeat is not double-reported

    limited = list(backfill.plan_updates(repo, "main", limit=2))
    assert len(limited) == 2


def test_plan_updates_ignores_prs_that_are_not_a_single_entry_pair():
    repo = _Repo([_PR(7, "smith_paper_2025", [_File("tools/foo.py")])], {})
    assert list(backfill.plan_updates(repo, "main")) == []


def test_plan_updates_survives_an_unreadable_pr():
    entry = _entry("Paper", url="https://arxiv.org/abs/2401.12345")
    good = _repo_for([entry], ["smith_paper_2025"])
    # A second PR whose files exist in the PR but not in the repo's content map.
    bad = _PR(99, "ghost_paper_2025", [_File("entries/2025/ghost_paper_2025.bib")])
    good._pulls.append(bad)

    planned = list(backfill.plan_updates(good, "main"))

    assert [pr.head.ref for pr, _, _ in planned] == ["smith_paper_2025"]  # the good one still lands


def test_plan_updates_can_target_a_single_pr():
    entries = [_entry(f"Paper {i}", url=f"https://arxiv.org/abs/2401.{i:05d}") for i in range(3)]
    repo = _repo_for(entries, [f"smith_paper_{i}_2025" for i in range(3)])
    target = repo.get_pulls()[1]

    planned = list(backfill.plan_updates(repo, "main", pr_number=target.number))

    assert [pr.number for pr, _, _ in planned] == [target.number]
