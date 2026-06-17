"""pr8 — same-shard S2 duplicate: a citekey re-added in the *same* year shard.

The complement of ``pr6`` (cross-shard). Here a stray drop carries a citekey that
already lives in the store at the *same* derived path — same citekey **and** same
year — so the planned target lands right on top of an existing, differently-worded
entry.

Like ``pr6``, this **fails loudly before any write** (a ``NormalizeError`` / non-zero
exit with a ``::error::`` annotation) instead of silently clobbering the stored entry
and losing a publication. In a real MR a literal same-path add is caught even earlier
by git's add/add conflict; but a drop placed under a stray filename (``incoming.bib``)
slips past git, so the diff job has to be the backstop. A human renames the key or
reconciles the two entries — the store never picks a winner on its own.

The one tolerated case is an *identical* re-drop (byte-for-byte the stored canonical
text): there is nothing to resolve, so it stays the ``pr3`` idempotent no-op.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_store, run_check, run_diff, run_diff_main
from publication_store.diff_job import NormalizeError

CITEKEY = "lorch_amortized_2025"
BIB_REL = ("entries", "2025", f"{CITEKEY}.bib")


def test_diff_job_fails_loudly_on_a_same_shard_overwrite(tmp_path, monkeypatch, capsys):
    # Existing entry already in the 2025 shard.
    make_store(tmp_path, [{"bib": load_fixture("pr8", "existing.bib")}])
    stored_before = tmp_path.joinpath(*BIB_REL).read_text(encoding="utf-8")
    # A later MR drops the same citekey + same year but with differing content
    # (an updated url) -> it derives the *same* path as the stored entry.
    drop = tmp_path / "incoming.bib"
    drop.write_text(load_fixture("pr8", "dup_drop.bib"), encoding="utf-8")

    # The pure core fails loudly (before writing anything).
    with pytest.raises(NormalizeError) as excinfo:
        run_diff(tmp_path, [drop])
    assert CITEKEY in str(excinfo.value)
    assert "same-shard" in str(excinfo.value)

    # Nothing was overwritten: the stored entry is byte-for-byte unchanged and the
    # raw drop is left in place for the human to resolve.
    assert tmp_path.joinpath(*BIB_REL).read_text(encoding="utf-8") == stored_before
    assert drop.exists()

    # The console script surfaces it as a non-zero exit with a ::error:: annotation.
    code, out = run_diff_main(tmp_path, [drop], monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out


def test_identical_same_shard_redrop_is_a_silent_noop(tmp_path):
    # Re-dropping a byte-for-byte match of the stored entry is the pr3 idempotent
    # case routed through a fresh drop: same path, identical canonical text, nothing
    # to resolve — so it is *not* treated as a collision.
    make_store(tmp_path, [{"bib": load_fixture("pr8", "existing.bib")}])
    drop = tmp_path / "incoming.bib"
    drop.write_text(load_fixture("pr8", "existing.bib"), encoding="utf-8")

    _, removed, warnings = run_diff(tmp_path, [drop])

    assert warnings == []
    assert drop in removed  # the raw drop is still cleaned up
    assert run_check(tmp_path) == []
