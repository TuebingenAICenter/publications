"""Smoke test for the compiled-artifacts add-on (``pubstore-compile``).

Manual developer harness, **not** wired into CI (consistent with the rest of
``tools/tests/`` — the store gate that runs in CI is ``pubstore-check``). Builds a
tiny green store (three entries: one co-owned by two groups, one by a single group,
one with no group), runs each compiler, and asserts the three joined views plus
idempotence.
"""

from __future__ import annotations

import json

from conftest import load_fixture, make_store
from publication_store import artifacts

# Three real, already-canonical fixtures (all in the 2025 shard).
TWO_GROUP = "lu_can_2025"      # custom.groups = [bethge, schoelkopf]
ONE_GROUP = "mueller_causal_2025"  # custom.groups = [schoelkopf]
NO_GROUP = "mahler_old_2025"   # custom.groups absent


def _seed(tmp_path):
    return make_store(
        tmp_path,
        [
            {"bib": load_fixture("pr3", "lu_can_2025.bib"), "custom": {"groups": ["bethge", "schoelkopf"]}},
            {"bib": load_fixture("pr4", "base.bib"), "custom": {"groups": ["schoelkopf"]}},
            {"bib": load_fixture("pr13", "base.bib"), "custom": {}},
        ],
    )


def test_all_bib_holds_every_entry_citekey_sorted(tmp_path):
    _seed(tmp_path)
    all_bib, warnings = artifacts.compile_all_bib(tmp_path)
    assert warnings == []
    # Every citekey is present.
    for key in (TWO_GROUP, ONE_GROUP, NO_GROUP):
        assert "{" + key + "," in all_bib
    # …and they appear citekey-sorted (lu_can < mahler_old < mueller_causal): the
    # positions, taken in sorted-key order, must be strictly ascending.
    positions = [all_bib.index("{" + key + ",") for key in sorted((TWO_GROUP, ONE_GROUP, NO_GROUP))]
    assert positions == sorted(positions)


def test_group_bibs_bucket_by_custom_groups(tmp_path):
    _seed(tmp_path)
    group_bibs, warnings = artifacts.compile_group_bibs(tmp_path)
    assert warnings == []
    assert set(group_bibs) == {"bethge", "schoelkopf"}

    # The two-group entry appears in BOTH group bibs.
    assert "{" + TWO_GROUP + "," in group_bibs["bethge"]
    assert "{" + TWO_GROUP + "," in group_bibs["schoelkopf"]
    # The one-group entry only in schoelkopf.
    assert "{" + ONE_GROUP + "," in group_bibs["schoelkopf"]
    assert "{" + ONE_GROUP + "," not in group_bibs["bethge"]
    # The no-group entry is in NO group bib (but is still in all.bib, above).
    assert "{" + NO_GROUP + "," not in group_bibs["bethge"]
    assert "{" + NO_GROUP + "," not in group_bibs["schoelkopf"]


def test_meta_json_is_a_verbatim_sidecar_join(tmp_path):
    _seed(tmp_path)
    meta, warnings = artifacts.compile_meta_json(tmp_path)
    assert warnings == []
    assert set(meta) == {TWO_GROUP, ONE_GROUP, NO_GROUP}
    # Each value equals the entry's stored sidecar byte-for-byte (after json parse).
    for key in meta:
        stored = json.loads((tmp_path / "meta" / "2025" / f"{key}.json").read_text(encoding="utf-8"))
        assert meta[key] == stored


def test_cli_writes_all_three_and_is_idempotent(tmp_path, monkeypatch):
    _seed(tmp_path)
    out = tmp_path / "build"

    def compile_once() -> dict[str, bytes]:
        monkeypatch.setattr(
            "sys.argv", ["pubstore-compile", "--root", str(tmp_path), "--out", str(out)]
        )
        artifacts.main()
        return {
            str(p.relative_to(out)): p.read_bytes()
            for p in sorted(out.rglob("*"))
            if p.is_file()
        }

    first = compile_once()
    assert set(first) == {"all.bib", "meta.json", "groups/bethge.bib", "groups/schoelkopf.bib"}

    # meta.json is the keyed object, deterministic (sort_keys) and line-diffable.
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert set(meta) == {TWO_GROUP, ONE_GROUP, NO_GROUP}

    # Idempotence: re-running on the same commit is byte-identical.
    second = compile_once()
    assert first == second
