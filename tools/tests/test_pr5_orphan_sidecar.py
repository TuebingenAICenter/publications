"""pr5 — contributor moves the ``.bib`` but forgets the sidecar.

The deliberate "the fix is human" case. The contributor relocates the bib by hand
(``entries/2025/`` -> ``entries/2026/``) and leaves the sidecar behind at ``meta/2025/``.
The diff job does **not** auto-heal the orphan; the checker is what fails loudly (S3).
"""

from __future__ import annotations

from conftest import load_fixture, make_store, run_check, run_check_main, run_diff


def test_orphan_sidecar_is_not_healed_and_fails_the_checker(tmp_path, monkeypatch, capsys):
    make_store(tmp_path, [{"bib": load_fixture("pr5", "base.bib")}])

    old_bib = tmp_path / "entries" / "2025" / "mueller_causal_2025.bib"
    orphan_meta = tmp_path / "meta" / "2025" / "mueller_causal_2025.json"
    moved_bib = tmp_path / "entries" / "2026" / "mueller_causal_2025.bib"
    # The hand move: bib goes to the 2026 shard, the sidecar is left behind at 2025.
    moved_bib.parent.mkdir(parents=True, exist_ok=True)
    moved_bib.write_text(load_fixture("pr5", "moved.bib"), encoding="utf-8")
    old_bib.unlink()

    # The diff job runs on the moved file — it does not reach back to heal the orphan.
    run_diff(tmp_path, [moved_bib])
    assert orphan_meta.exists(), "diff job must not auto-heal the orphan sidecar"

    # The checker is the one that fails loudly (exit 1) on the orphan (S3).
    errors = run_check(tmp_path)
    assert any("orphan sidecar" in e for e in errors)
    assert any("meta/2025/mueller_causal_2025.json" in e for e in errors)

    code, out = run_check_main(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out
