"""pr10 — a root drop carrying its own sibling ``.json`` (the zotero overlay).

The agent/zotero-overlay submission path. A contributor (or the scraper) drops
``paper.bib`` next to ``paper.json``, where the ``.json`` is the ``{citekey: overlay}``
map fed back into ``from_bibtex`` so the lossless ``zotero`` fields BibTeX can't carry
(``collections``, ``notes``, …) survive the round-trip. The diff job places the entry,
folds the overlay into the written sidecar's **zotero** half, and **deletes the sibling**
(S5 — it is a raw source, not a stored pair).

Pinned contract: a sibling ``.json`` populates **only** the ``zotero`` half; the
``custom`` half of a fresh drop is always ``{}`` (the store never fabricates report
fields — those are an agent's / a later job's to populate). To ship a ``custom`` half a
submitter places the pair at its canonical ``meta/`` path instead (the in-store path),
which is the pr3/pr4 territory.
"""

from __future__ import annotations

import json

from conftest import load_fixture, make_store, run_check, run_diff


def test_sibling_json_overlay_is_folded_in_and_deleted(tmp_path):
    make_store(tmp_path, [])  # empty store
    drop = tmp_path / "paper.bib"
    sibling = tmp_path / "paper.json"
    drop.write_text(load_fixture("pr10", "drop.bib"), encoding="utf-8")
    sibling.write_text(load_fixture("pr10", "drop.json"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [drop])

    bib = tmp_path / "entries" / "2025" / "smith_overlay_2025.bib"
    meta = tmp_path / "meta" / "2025" / "smith_overlay_2025.json"
    assert bib.exists() and meta.exists()
    assert set(written) == {bib, meta}
    assert warnings == []

    # S5: both the raw drop *and* its sibling map are cleaned up.
    assert not drop.exists() and not sibling.exists()
    assert drop in removed and sibling in removed

    sidecar = json.loads(meta.read_text(encoding="utf-8"))
    # The overlay survives losslessly in the zotero half...
    assert sidecar["zotero"]["collections"] == ["#collection_demo"]
    assert sidecar["zotero"]["notes"][0]["note"] == "<p>scraped overlay carried losslessly</p>"
    # ...and the custom half is left empty — never fabricated from a drop.
    assert sidecar["custom"] == {}

    assert run_check(tmp_path) == []
