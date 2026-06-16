"""pr3 — an already-canonical, correctly-placed entry is an idempotent no-op.

The regression guard for *both* fixpoint checks: the diff job's idempotency self-check
(re-normalizing its own output changes nothing) and the checker's canonical-form check.
Assert both report "nothing to do".
"""

from __future__ import annotations

import json

from conftest import load_fixture, make_store, run_check, run_diff


def test_canonical_entry_is_a_noop_for_both_tools(tmp_path):
    fixture_sidecar = json.loads(load_fixture("pr3", "lu_can_2025.json"))
    make_store(
        tmp_path,
        [{"bib": load_fixture("pr3", "lu_can_2025.bib"), "zotero": fixture_sidecar["zotero"], "custom": fixture_sidecar["custom"]}],
    )
    bib = tmp_path / "entries" / "2025" / "lu_can_2025.bib"
    meta = tmp_path / "meta" / "2025" / "lu_can_2025.json"
    bib_before = bib.read_text(encoding="utf-8")
    meta_before = meta.read_text(encoding="utf-8")

    # Diff-job fixpoint: re-normalizing the canonical entry rewrites the same bytes,
    # removes nothing, and (crucially) does not raise the idempotency NormalizeError.
    written, removed, warnings = run_diff(tmp_path, [bib])
    assert removed == []
    assert warnings == []
    assert bib.read_text(encoding="utf-8") == bib_before
    assert meta.read_text(encoding="utf-8") == meta_before
    assert set(written) == {bib, meta}  # rewritten in place, byte-identical

    # Checker fixpoint: the read-only counterpart agrees there is nothing to do.
    assert run_check(tmp_path) == []
