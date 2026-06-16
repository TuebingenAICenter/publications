"""pr4 — preprint -> published, year 2025 -> 2026, via an in-place field edit.

A stored 2025 preprint is edited in place (the bib's year becomes 2026 and the type
changes). The diff job relocates the pair ``2025/`` -> ``2026/``, **carries the existing
``custom`` half verbatim**, refreshes the ``zotero`` half from the new item, deletes the old
pair, and re-canonicalizes the bib. The checker passes.
"""

from __future__ import annotations

import json

from conftest import load_fixture, make_store, run_check, run_diff


def test_relocation_carries_custom_and_refreshes_zotero(tmp_path):
    base_sidecar = json.loads(load_fixture("pr4", "base.json"))
    base_custom = base_sidecar["custom"]
    make_store(
        tmp_path,
        [{"bib": load_fixture("pr4", "base.bib"), "zotero": base_sidecar["zotero"], "custom": base_custom}],
    )

    old_bib = tmp_path / "entries" / "2025" / "mueller_causal_2025.bib"
    old_meta = tmp_path / "meta" / "2025" / "mueller_causal_2025.json"
    # The in-place edit: the contributor rewrites the bib (now year 2026) but does not
    # touch the sidecar — the diff job is responsible for moving + carrying it.
    old_bib.write_text(load_fixture("pr4", "edited.bib"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [old_bib])

    new_bib = tmp_path / "entries" / "2026" / "mueller_causal_2025.bib"
    new_meta = tmp_path / "meta" / "2026" / "mueller_causal_2025.json"
    # Relocated into the new year shard.
    assert new_bib.exists() and new_meta.exists()
    # Old pair deleted — no orphan, no duplicate.
    assert not old_bib.exists() and not old_meta.exists()
    assert old_bib in removed and old_meta in removed

    sidecar = json.loads(new_meta.read_text(encoding="utf-8"))
    # The custom half survives the move verbatim (status/provenance/mentions unchanged).
    assert sidecar["custom"] == base_custom
    # The zotero half is refreshed from the new item but stays lossless (notes carried).
    assert sidecar["zotero"]["notes"] == base_sidecar["zotero"]["notes"]

    # S4: the relocated bib is canonical and reflects the published metadata.
    new_text = new_bib.read_text(encoding="utf-8")
    assert "@inproceedings{mueller_causal_2025," in new_text
    assert "year = {2026}" in new_text
    assert run_check(tmp_path) == []
