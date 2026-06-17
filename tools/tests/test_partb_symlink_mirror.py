"""Part B — the ``groups/<slug>/`` symlink browse mirror (the group-directories add-on).

The mirror is a **derived view** of ``custom.groups`` (the single source of truth),
owned by its own push-to-main workflow — never part of the S1–S5 gate. ``rebuild_mirror``
wipes the ``groups/`` symlinks (never the committed ``.gitkeep`` skeleton) and recreates
``groups/<slug>/<citekey>.bib`` as a **relative** symlink into
``entries/<year>/<citekey>.bib`` from each entry's ``custom.groups``.

The cases the plan names:

1. **Build** — set ``custom.groups`` on two entries, ``rebuild-mirror`` → the symlink
   tree matches (relative links resolving to the real entries); idempotent on re-run.
2. **Flip a slug** — drop a slug from one entry, rebuild → the stale symlink is gone
   **but the ``.gitkeep`` stays**.
3. **Hand-broken symlink** — ``--check`` flags it (and missing / stale links); a clean
   tree passes.
4. **Closure blind to ``groups/``** — ``pubstore-check`` (S1–S5) stays green with a
   populated symlink tree present.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import (
    load_fixture,
    make_store,
    mirror_consistency_errors,
    rebuild_mirror,
    run_check,
    run_mirror_main,
    write_sidecar,
)
from publication_store import entry

A_KEY, A_YEAR = "wahl_group_2025", "2025"
B_KEY, B_YEAR = "lindholm_mirror_2026", "2026"


def _seed_two_entries(root: Path, a_groups, b_groups) -> None:
    make_store(
        root,
        [
            {"bib": load_fixture("partb", "paper_a.bib"), "custom": {"groups": a_groups}},
            {"bib": load_fixture("partb", "paper_b.bib"), "custom": {"groups": b_groups}},
        ],
    )


def _link(root: Path, slug: str, key: str) -> Path:
    return root / "groups" / slug / f"{key}.bib"


def test_rebuild_builds_relative_symlinks_to_entries(tmp_path):
    _seed_two_entries(tmp_path, ["bethge", "wang"], ["lindholm"])

    created, removed = rebuild_mirror(tmp_path)

    assert removed == []  # nothing to clear on a first build
    expected = {
        _link(tmp_path, "bethge", A_KEY): f"../../entries/{A_YEAR}/{A_KEY}.bib",
        _link(tmp_path, "wang", A_KEY): f"../../entries/{A_YEAR}/{A_KEY}.bib",
        _link(tmp_path, "lindholm", B_KEY): f"../../entries/{B_YEAR}/{B_KEY}.bib",
    }
    assert set(created) == set(expected)
    for link, target in expected.items():
        assert link.is_symlink()
        assert os.readlink(link) == target  # relative, survives clone/checkout
        # and resolves to the real entry
        assert link.resolve() == (link.parent / target).resolve()
        assert link.resolve().read_text(encoding="utf-8").startswith("@article{")

    assert mirror_consistency_errors(tmp_path) == []


def test_rebuild_is_idempotent(tmp_path):
    _seed_two_entries(tmp_path, ["bethge", "wang"], ["lindholm"])
    rebuild_mirror(tmp_path)
    before = {p: os.readlink(p) for p in (tmp_path / "groups").rglob("*") if p.is_symlink()}

    rebuild_mirror(tmp_path)  # a second clean rebuild reproduces byte-identical links
    after = {p: os.readlink(p) for p in (tmp_path / "groups").rglob("*") if p.is_symlink()}

    assert before == after
    assert mirror_consistency_errors(tmp_path) == []


def test_flip_slug_removes_stale_symlink_but_keeps_gitkeep(tmp_path):
    _seed_two_entries(tmp_path, ["bethge", "wang"], ["lindholm"])
    # Simulate the committed skeleton: a .gitkeep in each group folder.
    for slug in ("bethge", "wang", "lindholm"):
        keep = tmp_path / "groups" / slug / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("", encoding="utf-8")

    rebuild_mirror(tmp_path)
    assert _link(tmp_path, "wang", A_KEY).is_symlink()

    # Drop "wang" from entry A's custom.groups (a deliberate sidecar edit).
    _, meta_rel = entry.derive_path(A_YEAR, A_KEY)
    write_sidecar(tmp_path / meta_rel, {}, {"groups": ["bethge"]})

    created, removed = rebuild_mirror(tmp_path)

    # The stale wang/ symlink is gone; the bethge/ one remains.
    assert not _link(tmp_path, "wang", A_KEY).exists()
    assert _link(tmp_path, "bethge", A_KEY).is_symlink()
    # The .gitkeep skeleton is never touched — every group folder stays browsable.
    for slug in ("bethge", "wang", "lindholm"):
        assert (tmp_path / "groups" / slug / ".gitkeep").exists()
    assert mirror_consistency_errors(tmp_path) == []


def test_check_flags_hand_broken_symlink(tmp_path, monkeypatch, capsys):
    _seed_two_entries(tmp_path, ["bethge"], ["lindholm"])
    rebuild_mirror(tmp_path)
    assert mirror_consistency_errors(tmp_path) == []

    # Hand-break a mirror symlink: repoint it at a non-existent entry.
    link = _link(tmp_path, "bethge", A_KEY)
    link.unlink()
    link.symlink_to("../../entries/2025/does_not_exist.bib")

    errors = mirror_consistency_errors(tmp_path)
    assert any("broken symlink" in e and "bethge" in e for e in errors)

    code, out = run_mirror_main(tmp_path, monkeypatch, capsys, check=True)
    assert code == 1
    assert "::error::" in out
    assert "FAIL" in out


def test_check_flags_missing_link(tmp_path):
    _seed_two_entries(tmp_path, ["bethge"], ["lindholm"])
    rebuild_mirror(tmp_path)
    # Delete a link that custom.groups still expects -> a missing-link error.
    _link(tmp_path, "lindholm", B_KEY).unlink()

    errors = mirror_consistency_errors(tmp_path)
    assert any("missing mirror symlink" in e and B_KEY in e for e in errors)


def test_check_passes_via_cli(tmp_path, monkeypatch, capsys):
    _seed_two_entries(tmp_path, ["bethge", "wang"], ["lindholm"])
    rebuild_mirror(tmp_path)

    code, out = run_mirror_main(tmp_path, monkeypatch, capsys, check=True)
    assert code == 0
    assert "OK" in out


def test_checker_stays_green_with_populated_mirror(tmp_path):
    # S5 closure is blind to groups/: a fully populated symlink tree (+ .gitkeep)
    # must not trip pubstore-check.
    _seed_two_entries(tmp_path, ["bethge", "wang"], ["lindholm"])
    for slug in ("bethge", "wang", "lindholm"):
        keep = tmp_path / "groups" / slug / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("", encoding="utf-8")
    rebuild_mirror(tmp_path)

    assert run_check(tmp_path) == []
