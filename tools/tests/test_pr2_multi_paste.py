"""pr2 — multi-entry paste (10 entries) dropped at the repo root.

The diff job splits the paste **per parsed item** (``from_bibtex`` -> ``to_bibtex`` ->
per entry, never a blank-line text split) into one year-sharded canonical pair each, then
deletes the paste. The checker passes on the resulting store.
"""

from __future__ import annotations

from conftest import load_fixture, make_store, run_check, run_diff
from publication_store import entry

EXPECTED = {  # citekey -> year shard, derived from the parsed items
    "rutte_scaling_2026": "2026",
    "ormaniec_standardizing_2025": "2025",
    "qiu_orthogonal_2025": "2025",
    "zhang_towards_2025": "2025",
    "aggarwal_dars_2025": "2025",
    "lu_can_2025": "2025",
    "Qiuetal25b": "2025",
    "singh_directionality_2025": "2025",
    "kladny_conformal_2025": "2025",
    "simko_improving_2025": "2025",
}


def test_paste_is_split_per_item_and_cleaned(tmp_path):
    make_store(tmp_path, [])
    paste = tmp_path / "pubs.bib"
    paste.write_text(load_fixture("pr2", "paste.bib"), encoding="utf-8")

    written, removed, warnings = run_diff(tmp_path, [paste])

    # One sharded (.bib, .json) pair per parsed item.
    assert len(written) == 2 * len(EXPECTED)
    for citekey, year in EXPECTED.items():
        bib_rel, meta_rel = entry.derive_path(year, citekey)
        assert (tmp_path / bib_rel).exists(), f"missing {bib_rel}"
        assert (tmp_path / meta_rel).exists(), f"missing {meta_rel}"
    # S5: the paste itself is removed.
    assert not paste.exists()
    assert paste in removed
    assert warnings == []
    assert run_check(tmp_path) == []
