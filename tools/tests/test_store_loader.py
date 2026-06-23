"""Tests for the filesystem layer (``store``): walk, pair, and load the tree.

Manual developer harness, **not** wired into CI (consistent with the rest of
``tools/tests/`` — the gate that runs in CI is ``pubstore-check``). Seeds a tiny
green store with :func:`conftest.make_store`, then checks:

* the path primitives (``bib_paths`` / ``meta_paths`` / ``meta_path_for``) agree on
  the S1 layout the diff job writes;
* ``load_store_entries`` lifts the on-disk pairs into ``StoreEntry``\\ s verbatim;
* ``load_items`` composes that with the pure ``zotero_bridge`` half into the full
  disk → ``ZoteroItem`` round-trip, reconstructing ``custom.groups`` collections.
"""

from __future__ import annotations

from conftest import load_fixture, make_store
from publication_store import store, zotero_bridge

TWO_GROUP = "lu_can_2025"          # custom.groups = [bethge, schoelkopf]
ONE_GROUP = "mueller_causal_2025"  # custom.groups = [schoelkopf]
NO_GROUP = "mahler_old_2025"       # custom.groups absent


def _seed(tmp_path):
    return make_store(
        tmp_path,
        [
            {"bib": load_fixture("pr3", "lu_can_2025.bib"), "custom": {"groups": ["bethge", "schoelkopf"]}},
            {"bib": load_fixture("pr4", "base.bib"), "custom": {"groups": ["schoelkopf"]}},
            {"bib": load_fixture("pr13", "base.bib"), "custom": {}},
        ],
    )


def test_path_primitives_pair_the_s1_layout(tmp_path):
    _seed(tmp_path)
    bibs = store.bib_paths(tmp_path)
    metas = store.meta_paths(tmp_path)

    assert [p.stem for p in bibs] == sorted([TWO_GROUP, ONE_GROUP, NO_GROUP])
    # Every walked .bib pairs to an existing sidecar at the same shard + stem.
    for bib_path in bibs:
        meta_path = store.meta_path_for(bib_path, tmp_path)
        assert meta_path.exists()
        assert meta_path.parent.name == bib_path.parent.name  # same year shard
        assert meta_path.stem == bib_path.stem                # same citekey
    # meta_paths() is exactly the set of paired sidecars.
    assert {store.meta_path_for(b, tmp_path) for b in bibs} == set(metas)


def test_load_store_entries_lifts_pairs_verbatim(tmp_path):
    _seed(tmp_path)
    entries = {e.citekey: e for e in store.load_store_entries(tmp_path)}
    assert set(entries) == {TWO_GROUP, ONE_GROUP, NO_GROUP}

    # The bib text + sidecar are read straight off disk, byte-for-byte.
    for citekey, e in entries.items():
        bib_path = tmp_path / "entries" / "2025" / f"{citekey}.bib"
        assert e.bib == bib_path.read_text(encoding="utf-8")
        assert set(e.sidecar) == {"zotero", "custom"}

    assert entries[TWO_GROUP].sidecar["custom"]["groups"] == ["bethge", "schoelkopf"]
    assert entries[NO_GROUP].sidecar["custom"] == {}


def test_load_items_round_trips_the_tree_to_zotero(tmp_path):
    _seed(tmp_path)
    items, collections = store.load_items(tmp_path)

    assert len(items) == 3
    # custom.groups is rebuilt into collections by name.
    assert {c.name for c in collections} == {"bethge", "schoelkopf"}

    # load_items == from_store_entries(load_store_entries(...)) by construction.
    direct = zotero_bridge.from_store_entries(store.load_store_entries(tmp_path))
    assert {i.title for i in items} == {i.title for i in direct[0]}
    assert {c.name for c in collections} == {c.name for c in direct[1]}
