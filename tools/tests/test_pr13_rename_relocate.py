"""pr13 — citekey-*rename* relocation (edit the key, same year).

The other relocation axis from ``pr4`` (which moved across *years*). Here a stored entry
keeps its year but its **citekey** is edited in place — a human fixes a typo'd key, or
aligns it to the generator's scheme. The derived path changes within the same year shard
(``2025/mahler_old_2025`` → ``2025/mahler_causal_2025``), so the diff job must relocate
the pair, **carry the ``custom`` half**, refresh the ``zotero`` half against the renamed
entry, and delete the old pair — no orphan, no duplicate, no lost report data. Mirrors
pr4's guarantees on the citekey axis instead of the year axis.
"""

from __future__ import annotations

import json

from conftest import load_fixture, make_store, run_check, run_diff


def test_citekey_rename_relocates_and_carries_custom(tmp_path):
    base_sidecar = json.loads(load_fixture("pr13", "base.json"))
    make_store(
        tmp_path,
        [{"bib": load_fixture("pr13", "base.bib"), "zotero": base_sidecar["zotero"], "custom": base_sidecar["custom"]}],
    )

    old_bib = tmp_path / "entries" / "2025" / "mahler_old_2025.bib"
    old_meta = tmp_path / "meta" / "2025" / "mahler_old_2025.json"
    # In-place edit: the contributor rewrites only the citekey (same year, same fields).
    old_bib.write_text(load_fixture("pr13", "edited.bib"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [old_bib])

    new_bib = tmp_path / "entries" / "2025" / "mahler_causal_2025.bib"
    new_meta = tmp_path / "meta" / "2025" / "mahler_causal_2025.json"
    # Relocated within the same year shard to the renamed key.
    assert new_bib.exists() and new_meta.exists()
    # Old pair deleted — no orphan, no duplicate key across shards.
    assert not old_bib.exists() and not old_meta.exists()
    assert old_bib in removed and old_meta in removed
    assert warnings == []

    sidecar = json.loads(new_meta.read_text(encoding="utf-8"))
    # The custom half rides along verbatim; the zotero half stays lossless.
    assert sidecar["custom"] == base_sidecar["custom"]
    assert sidecar["zotero"]["notes"] == base_sidecar["zotero"]["notes"]

    assert "@article{mahler_causal_2025," in new_bib.read_text(encoding="utf-8")
    assert run_check(tmp_path) == []
