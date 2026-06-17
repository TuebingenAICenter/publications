"""pr12 — S5 closure: the only files under ``entries/``/``meta/`` are the pairs.

``pr5`` covers one closure violation (an orphan sidecar); this exercises the rest of
the checker's :func:`closure_errors`. Starting from one valid entry, we drop in the
shapes the gate must reject and assert each is flagged (and that the valid pair is *not*
a false positive):

* a non-``.bib`` file under ``entries/`` (suffix mismatch);
* a non-``.json`` file under ``meta/`` (suffix mismatch);
* a file at the wrong nesting depth under ``meta/`` (not ``<year>/<stem>``);
* a stray ``.bib`` / leftover monolith at the repo root.
"""

from __future__ import annotations

from conftest import load_fixture, make_store, run_check


def test_closure_flags_every_stray_shape(tmp_path):
    # One valid, canonical entry — the closure check must leave it alone.
    make_store(tmp_path, [{"bib": load_fixture("pr1", "drop.bib")}])
    assert run_check(tmp_path) == []  # baseline: a clean store passes

    loose_json = tmp_path / "entries" / "2025" / "orphan_note.json"
    loose_json.write_text("{}\n", encoding="utf-8")
    meta_bib = tmp_path / "meta" / "2025" / "stray_entry.bib"
    meta_bib.write_text("@misc{x,\n}\n", encoding="utf-8")
    shallow_meta = tmp_path / "meta" / "loose_at_root.json"
    shallow_meta.write_text("{}\n", encoding="utf-8")
    root_monolith = tmp_path / "tueai_publications.bib"
    root_monolith.write_text("@misc{leftover,\n}\n", encoding="utf-8")

    errors = run_check(tmp_path)

    def flagged(needle: str) -> bool:
        return any(needle in e for e in errors)

    # Each stray is reported as a closure violation against its location.
    assert flagged("entries/2025/orphan_note.json") and flagged("unexpected file under entries/")
    assert flagged("meta/2025/stray_entry.bib") and flagged("unexpected file under meta/")
    assert flagged("meta/loose_at_root.json")
    assert flagged("tueai_publications.bib") and flagged("stray file at the repo root")

    # The valid entry is not implicated in any message.
    assert not flagged("samway_are_2025")
