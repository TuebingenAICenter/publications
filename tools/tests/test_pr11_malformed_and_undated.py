"""pr11 — two narrow edge channels: a malformed ``.bib`` and an undated entry.

* **Malformed input.** A ``.bib`` that does not parse is *not* a store-invariant
  failure (``NormalizeError``) but a plain bad-input ``ValueError``. The console script
  routes it to its own channel — ``::error::normalize aborted (malformed input, nothing
  written)`` and a non-zero exit — and writes nothing. This is the third distinct exit
  path, alongside a valid no-op and a loud S2/idempotency failure.

* **Undated entry.** An entry with no ``year`` field is legitimate: it shards under
  ``entries/undated/`` (the ``UNDATED`` placeholder), not dropped or rejected. The
  checker accepts the result — ``undated`` is a normal year shard.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_store, run_check, run_diff, run_diff_main


def test_malformed_bib_aborts_with_nothing_written(tmp_path, monkeypatch, capsys):
    make_store(tmp_path, [])
    bad = tmp_path / "broken.bib"
    bad.write_text(load_fixture("pr11", "malformed.bib"), encoding="utf-8")

    # The pure core raises ValueError (bad input) — distinct from NormalizeError.
    with pytest.raises(ValueError):
        run_diff(tmp_path, [bad])

    # Nothing was written and the raw file is untouched.
    entries = tmp_path / "entries"
    assert not entries.exists() or list(entries.rglob("*.bib")) == []
    assert bad.exists()

    # The console script reports the malformed-input channel and exits non-zero.
    code, out = run_diff_main(tmp_path, [bad], monkeypatch, capsys)
    assert code == 1
    assert "malformed input" in out


def test_undated_entry_shards_under_undated(tmp_path):
    make_store(tmp_path, [])
    drop = tmp_path / "no_year.bib"
    drop.write_text(load_fixture("pr11", "undated.bib"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [drop])

    bib = tmp_path / "entries" / "undated" / "turing_machinery.bib"
    meta = tmp_path / "meta" / "undated" / "turing_machinery.json"
    assert bib.exists() and meta.exists()
    assert set(written) == {bib, meta}
    assert not drop.exists() and drop in removed
    assert warnings == []
    assert run_check(tmp_path) == []
