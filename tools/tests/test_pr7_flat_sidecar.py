"""pr7 — a flat / legacy sidecar (data in top-level fields, not under zotero/custom).

The reject-not-migrate contract (S4). A sidecar whose data sits in top-level fields is
**rejected, never silently migrated** — the checker fails loudly, and the diff job raises
rather than read it as empty (which would drop the whole overlay). In both cases the source
bytes must be untouched.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_store, run_check, run_check_main, run_diff, run_diff_main

CITEKEY = "kladny_conformal_2025"


def _seed_flat_sidecar(tmp_path):
    """Canonical bib in the store, but its sidecar is the flat/legacy shape."""
    make_store(tmp_path, [{"bib": load_fixture("pr7", "entry.bib")}])
    bib = tmp_path / "entries" / "2025" / f"{CITEKEY}.bib"
    meta = tmp_path / "meta" / "2025" / f"{CITEKEY}.json"
    flat = load_fixture("pr7", "flat.json")
    meta.write_text(flat, encoding="utf-8")  # overwrite the valid sidecar with the flat one
    return bib, meta, flat


def test_checker_rejects_the_flat_sidecar_without_touching_it(tmp_path, monkeypatch, capsys):
    bib, meta, flat = _seed_flat_sidecar(tmp_path)

    errors = run_check(tmp_path)
    assert any("Additional properties are not allowed" in e for e in errors)
    assert any(f"meta/2025/{CITEKEY}.json" in e for e in errors)

    code, out = run_check_main(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out
    # Read-only: the flat sidecar bytes are unchanged (no silent rewrite).
    assert meta.read_text(encoding="utf-8") == flat


def test_diff_job_rejects_not_migrates_the_flat_sidecar(tmp_path, monkeypatch, capsys):
    bib, meta, flat = _seed_flat_sidecar(tmp_path)
    bib_before = bib.read_text(encoding="utf-8")

    # Re-normalizing the in-store entry must raise on the bad sidecar (split_sidecar),
    # not migrate it — a valid input whose sidecar the store refuses.
    with pytest.raises(ValueError):
        run_diff(tmp_path, [bib])
    # Reject-not-migrate: nothing was rewritten.
    assert meta.read_text(encoding="utf-8") == flat
    assert bib.read_text(encoding="utf-8") == bib_before

    code, out = run_diff_main(tmp_path, [bib], monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out
    assert meta.read_text(encoding="utf-8") == flat
