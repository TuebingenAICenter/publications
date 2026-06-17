"""pr9 — within-MR duplicate: two changed files collide on one derived path.

The within-MR sibling of ``pr8``. Two *different* publications in the **same** change
were hand-assigned the same citekey and year, so both canonicalize to one derived path
``entries/<year>/<citekey>.bib``. Neither exists in the store yet, so the on-disk
collision checks (``pr6``/``pr8``) can't see it; git can't either (two source
filenames, no add/add conflict). Left unchecked the second write silently clobbers the
first and *both* raw drops are deleted — a publication lost with zero signal, and the
checker would still pass (only one file lands).

So the diff job's batch check **fails loudly before any write**, the same disposition
as pr8: a human renames one key or drops the duplicate. (A *single* file with duplicate
keys is rejected even earlier, by the BibTeX parser — that is the ``ValueError`` path,
not this one.)
"""

from __future__ import annotations

import pytest

from conftest import load_fixture, make_store, run_diff, run_diff_main
from publication_store.diff_job import NormalizeError

CITEKEY = "kovac_emergent_2025"
BIB_REL = ("entries", "2025", f"{CITEKEY}.bib")


def test_two_files_colliding_on_one_path_fail_loudly(tmp_path, monkeypatch, capsys):
    make_store(tmp_path, [])  # empty store
    first = tmp_path / "first.bib"
    second = tmp_path / "second.bib"
    first.write_text(load_fixture("pr9", "first.bib"), encoding="utf-8")
    second.write_text(load_fixture("pr9", "second.bib"), encoding="utf-8")

    # The pure core fails loudly before writing anything.
    with pytest.raises(NormalizeError) as excinfo:
        run_diff(tmp_path, [first, second])
    assert CITEKEY in str(excinfo.value)
    assert "collide on one path" in str(excinfo.value)

    # No write happened and *both* raw drops are left in place for the human.
    assert not tmp_path.joinpath(*BIB_REL).exists()
    assert first.exists() and second.exists()

    # The console script surfaces it as a non-zero exit with a ::error:: annotation.
    code, out = run_diff_main(tmp_path, [first, second], monkeypatch, capsys)
    assert code == 1
    assert "::error::" in out
