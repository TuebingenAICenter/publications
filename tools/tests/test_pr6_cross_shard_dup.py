"""pr6 — cross-shard S2 duplicate, the *sequential* case.

A citekey already in ``main`` (here in ``2025/``) is re-added in a different year shard
(``2026/``) by a *later* MR — a hand-edited key the filesystem can't catch on its own (the
two paths differ). The diff job's diff-scoped 1xN collision check must see the existing key
and fail loudly. The *concurrent* variant stays deliberately ungated (a transient
duplicate) and is not tested as a failure.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_store, run_check, run_diff, run_diff_main
from publication_store.diff_job import NormalizeError

CITEKEY = "lorch_amortized_2025"


def test_diff_job_collision_check_catches_the_cross_shard_dup(tmp_path, monkeypatch, capsys):
    # Existing entry already in the 2025 shard.
    make_store(tmp_path, [{"bib": load_fixture("pr6", "existing.bib")}])
    # A later MR drops the same citekey but with year 2026 -> would land in the 2026 shard.
    drop = tmp_path / "incoming.bib"
    drop.write_text(load_fixture("pr6", "dup_drop.bib"), encoding="utf-8")

    # The pure core fails loudly (before writing anything).
    with pytest.raises(NormalizeError) as excinfo:
        run_diff(tmp_path, [drop])
    assert CITEKEY in str(excinfo.value)
    assert "cross-shard" in str(excinfo.value)

    # Nothing was written: the 2026 shard does not exist, the drop is untouched.
    assert not (tmp_path / "entries" / "2026" / f"{CITEKEY}.bib").exists()
    assert drop.exists()

    # The console script surfaces it as a non-zero exit with a ::error:: annotation.
    code, out = run_diff_main(tmp_path, [drop], monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out


def test_full_checker_also_catches_the_stem_collision_if_both_landed(tmp_path):
    # If both shards somehow landed (the concurrent case), the full checker's O(N)
    # filename check flags the stem appearing in two shards (S2).
    make_store(
        tmp_path,
        [
            {"bib": load_fixture("pr6", "existing.bib")},  # 2025
            {"bib": load_fixture("pr6", "dup_drop.bib")},  # 2026
        ],
    )
    errors = run_check(tmp_path)
    assert any(CITEKEY in e and "shards" in e for e in errors)
