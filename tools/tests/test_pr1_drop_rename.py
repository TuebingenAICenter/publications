"""pr1 — git-native single ``.bib`` drop at root, **filename != citekey**.

A contributor drops ``my_paper.bib`` (named nothing like the citekey) at the repo root.
The diff job must place it at its *derived* path under the year shard, rename the pair to
the citekey, and delete the original drop (S1, S4, S5). The checker then passes.
"""

from __future__ import annotations

from conftest import load_fixture, make_store, run_check, run_diff


def test_drop_is_placed_renamed_and_cleaned(tmp_path):
    make_store(tmp_path, [])  # empty store, schema installed
    drop = tmp_path / "my_paper.bib"  # filename deliberately != citekey samway_are_2025
    drop.write_text(load_fixture("pr1", "drop.bib"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [drop])

    bib = tmp_path / "entries" / "2025" / "samway_are_2025.bib"
    meta = tmp_path / "meta" / "2025" / "samway_are_2025.json"
    # S1: placed at the derived path under the year shard, renamed to the citekey.
    assert bib.exists() and meta.exists()
    # S5: the loosely-named drop is gone.
    assert not drop.exists()
    assert drop in removed
    assert set(written) == {bib, meta}
    assert warnings == []
    # S4: the placed entry is the canonical fixpoint.
    assert "@inproceedings{samway_are_2025," in bib.read_text(encoding="utf-8")
    # checker agrees the store is valid.
    assert run_check(tmp_path) == []
