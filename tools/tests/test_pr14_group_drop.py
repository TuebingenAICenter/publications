"""pr14 — implicit group association on drop (the ``groups/<slug>/`` add-on, Part A).

A contributor drops a ``.bib`` into ``groups/<slug>/`` and ``pubstore-groups
associate`` must: place the entry canonically under ``entries/<year>/<key>.bib`` (via
the *untouched* core ``normalize_changed``), **union** ``<slug>`` into the sidecar's
``custom.groups``, and delete the drop — ``groups/`` is a transient inbox.

The four cases the plan names:

1. **Create** — a new key → entry placed, ``custom.groups == ["bethge"]``, drop gone.
2. **Add existing (identical)** — a second drop of the *same* entry into another
   folder → unions ``wang``, keeps ``bethge``, entry content untouched (the
   move-into-group gesture).
3. **Reject on change** — a drop with the same key but a *changed* field → fails
   loudly, the stored entry and its groups untouched (a group drop is never a
   backdoor edit).
4. **Multi-group in one MR (the collapse)** — the same paper dropped into two folders
   in one change → collapsed to one derived path, normalized once,
   ``custom.groups == ["bethge", "wang"]``, both drops deleted — **not** rejected by
   the within-MR ``pr9`` batch guard.

Plus the Phase A3 closure check: ``pubstore-check`` (S1–S5) stays green with a
``groups/`` directory present — the gate is blind to ``groups/``.
"""

from __future__ import annotations

import pytest

from conftest import (
    drop_into_group,
    groups_of,
    load_fixture,
    make_store,
    run_associate,
    run_check,
    run_groups_main,
)
from publication_store import entry
from publication_store.diff_job import NormalizeError

CITEKEY = "wahl_group_2025"
YEAR = "2025"


def _bib_rel():
    return entry.derive_path(YEAR, CITEKEY)[0]


def test_create_places_entry_and_unions_slug(tmp_path):
    make_store(tmp_path, [])  # empty store; just installs the schema
    drop = drop_into_group(tmp_path, "bethge", "whatever.bib", load_fixture("pr14", "paper.bib"))

    written, removed, messages = run_associate(tmp_path, [drop])

    assert (tmp_path / _bib_rel()).exists()
    assert groups_of(tmp_path, YEAR, CITEKEY) == ["bethge"]
    assert not drop.exists() and drop in removed
    assert messages == [f"created {CITEKEY} in group(s) bethge"]
    assert run_check(tmp_path) == []


def test_add_existing_unchanged_drop_unions_without_touching_entry(tmp_path):
    # Seed the stored entry already in group "bethge".
    make_store(tmp_path, [{"bib": load_fixture("pr14", "paper.bib"), "custom": {"groups": ["bethge"]}}])
    stored_before = (tmp_path / _bib_rel()).read_text(encoding="utf-8")
    # An *identical* re-drop (after canonicalization) into another folder = move-into-group.
    drop = drop_into_group(tmp_path, "wang", "copy.bib", load_fixture("pr14", "paper.bib"))

    _, removed, messages = run_associate(tmp_path, [drop])

    assert groups_of(tmp_path, YEAR, CITEKEY) == ["bethge", "wang"]  # unioned, sorted
    assert (tmp_path / _bib_rel()).read_text(encoding="utf-8") == stored_before  # untouched
    assert not drop.exists() and drop in removed
    assert messages == [f"added {CITEKEY} to group(s) wang"]
    assert run_check(tmp_path) == []


def test_changed_content_drop_fails_loudly(tmp_path, monkeypatch, capsys):
    make_store(tmp_path, [{"bib": load_fixture("pr14", "paper.bib"), "custom": {"groups": ["bethge"]}}])
    stored_before = (tmp_path / _bib_rel()).read_text(encoding="utf-8")
    # Same key, a real field change (different url) -> a backdoor edit, must be refused.
    drop = drop_into_group(tmp_path, "x", "edit.bib", load_fixture("pr14", "changed.bib"))

    with pytest.raises(NormalizeError) as excinfo:
        run_associate(tmp_path, [drop])
    msg = str(excinfo.value)
    assert CITEKEY in msg
    assert "drop it UNCHANGED" in msg  # the reframed, group-aware guidance

    # Nothing changed: stored entry, its groups, and the drop are all left as-is.
    assert (tmp_path / _bib_rel()).read_text(encoding="utf-8") == stored_before
    assert groups_of(tmp_path, YEAR, CITEKEY) == ["bethge"]
    assert drop.exists()

    code, out = run_groups_main(tmp_path, [drop], monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out


def test_same_paper_into_two_folders_collapses_not_pr9(tmp_path):
    make_store(tmp_path, [])
    # The same paper dropped into TWO group folders in one MR. Both derive one path;
    # the collapse must union the slugs instead of tripping the pr9 batch guard.
    d1 = drop_into_group(tmp_path, "bethge", "a.bib", load_fixture("pr14", "paper.bib"))
    d2 = drop_into_group(tmp_path, "wang", "b.bib", load_fixture("pr14", "paper.bib"))

    written, removed, messages = run_associate(tmp_path, [d1, d2])

    assert (tmp_path / _bib_rel()).exists()
    assert groups_of(tmp_path, YEAR, CITEKEY) == ["bethge", "wang"]
    assert not d1.exists() and not d2.exists()
    assert d1 in removed and d2 in removed
    assert messages == [f"created {CITEKEY} in group(s) bethge, wang"]
    assert run_check(tmp_path) == []


def test_two_different_papers_same_key_in_one_mr_fails(tmp_path):
    make_store(tmp_path, [])
    # Two *different* papers hand-assigned the same key+year, dropped into two folders:
    # a real within-MR clash (pr9's intent), surfaced loudly before any write.
    d1 = drop_into_group(tmp_path, "bethge", "a.bib", load_fixture("pr14", "paper.bib"))
    d2 = drop_into_group(tmp_path, "wang", "b.bib", load_fixture("pr14", "changed.bib"))

    with pytest.raises(NormalizeError) as excinfo:
        run_associate(tmp_path, [d1, d2])
    assert CITEKEY in str(excinfo.value)
    # Nothing written.
    assert not (tmp_path / _bib_rel()).exists()


def test_symlink_under_groups_is_ignored(tmp_path):
    # A symlink under groups/ (the future browse-mirror) is never an inbox drop:
    # associate ignores it, writing nothing.
    make_store(tmp_path, [{"bib": load_fixture("pr14", "paper.bib")}])
    link = tmp_path / "groups" / "bethge" / f"{CITEKEY}.bib"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(tmp_path / _bib_rel())

    written, removed, messages = run_associate(tmp_path, [link])

    assert (written, removed, messages) == ([], [], [])
    assert link.is_symlink()  # untouched


def test_checker_is_blind_to_groups_directory(tmp_path, monkeypatch, capsys):
    # Phase A3: S5 closure scans only entries/ + meta/ + the repo root, never groups/.
    # A populated groups/ inbox (and the committed .gitkeep skeleton) must not trip it.
    make_store(tmp_path, [{"bib": load_fixture("pr14", "paper.bib")}])
    (tmp_path / "groups" / "bethge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "groups" / "bethge" / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "groups" / "wang" / "leftover.bib").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "groups" / "wang" / "leftover.bib").write_text(load_fixture("pr14", "paper.bib"), encoding="utf-8")

    assert run_check(tmp_path) == []
