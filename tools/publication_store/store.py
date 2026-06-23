"""Filesystem layer: walk, pair, and load the on-disk store tree.

The one module that knows the store *lives on disk* as the S1 layout

    entries/<year>/<citekey>.bib    one canonical BibTeX entry per file
    meta/<year>/<citekey>.json      matching sidecar, {"zotero": ..., "custom": ...}

Everything above this speaks in-memory units. The round-trip is two layers:

* :mod:`publication_store.zotero_bridge` — **pure**, in-memory:
  :class:`~publication_store.zotero_bridge.StoreEntry` ⇄ ``ZoteroItem``.
* this module — the **filesystem** half: ``StoreEntry`` ⇄ disk.

They compose into the full repo-tree ⇄ ``ZoteroItem`` round-trip
(:func:`load_items`), the inverse of what the diff job writes.

Two tiers, on purpose:

* **Path primitives** — :func:`bib_paths`, :func:`meta_paths`, :func:`meta_path_for`.
  The low-level vocabulary of the S1 walk + pairing, used by *every* store consumer
  including the checker. The checker can only use these, never the loaders below:
  :func:`load_store_entries` / :func:`load_items` *parse*, and a parse error raises —
  whereas the checker's whole job is to **report** an entry that won't parse, not
  abort on it. So the gate sits on the primitives; the green-store consumers (the
  artifact + group compilers, and any caller that just wants the items) sit on the
  loaders.

* **Loaders** — :func:`load_store_entries` (disk → ``StoreEntry``) and
  :func:`load_items` (disk → ``(items, collections)``). These assume an already-green
  store (run them on ``main`` after the gate): a malformed sidecar / un-parseable bib
  raises rather than warns.
"""

from __future__ import annotations

import json
from pathlib import Path

from zotero_rdf import ZoteroCollection, ZoteroItem

from .zotero_bridge import StoreEntry, from_store_entries


def bib_paths(root: Path) -> list[Path]:
    """Every stored entry ``entries/**/*.bib``, sorted (deterministic walk)."""
    return sorted((Path(root) / "entries").rglob("*.bib"))


def meta_paths(root: Path) -> list[Path]:
    """Every stored sidecar ``meta/**/*.json``, sorted (deterministic walk)."""
    return sorted((Path(root) / "meta").rglob("*.json"))


def meta_path_for(bib_path: Path, root: Path) -> Path:
    """The sidecar paired with ``bib_path`` (S1: same shard + stem).

    The inverse of the entries→meta pairing the diff job writes: an entry at
    ``entries/<shard>/<stem>.bib`` is paired with ``meta/<shard>/<stem>.json``. Pure
    path arithmetic — it does not check existence (that bijection is S3, the
    checker's job).
    """
    bib_path = Path(bib_path)
    return Path(root) / "meta" / bib_path.parent.name / f"{bib_path.stem}.json"


def load_store_entries(root: Path) -> list[StoreEntry]:
    """Read the on-disk store into ``list[StoreEntry]`` — the disk → ``StoreEntry`` half.

    One :class:`~publication_store.zotero_bridge.StoreEntry` per ``entries/**/*.bib``,
    its ``citekey`` the filename stem (== the entry's citekey by S1), its ``bib`` the
    file text, and its ``sidecar`` the paired ``meta/`` JSON verbatim
    (``{"zotero": …, "custom": …}``). A missing sidecar yields both halves empty —
    but on a green store (S3) every entry is paired, so that is only a defensive
    default. Assumes the store is valid: a sidecar that is not JSON raises, matching
    the loader contract (run after the gate). Symmetric with what the diff job emits,
    so ``from_store_entries(load_store_entries(root))`` reconstructs the library.
    """
    root = Path(root)
    entries: list[StoreEntry] = []
    for bib_path in bib_paths(root):
        meta_path = meta_path_for(bib_path, root)
        sidecar = (
            json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists()
            else {"zotero": {}, "custom": {}}
        )
        entries.append(
            StoreEntry(
                citekey=bib_path.stem,
                bib=bib_path.read_text(encoding="utf-8"),
                sidecar=sidecar,
            )
        )
    return entries


def load_items(root: Path) -> tuple[list[ZoteroItem], list[ZoteroCollection]]:
    """Load the whole store as ``(items, collections)`` — disk → ``ZoteroItem`` library.

    The full repo-tree ⇄ Zotero round-trip: :func:`load_store_entries` lifts the tree
    into ``StoreEntry``\\ s, then :func:`~publication_store.zotero_bridge.from_store_entries`
    overlays the ``zotero`` half and rebuilds the ``custom.groups`` collections +
    ``mentionsAICenter`` tags. The inverse of
    :func:`~publication_store.zotero_bridge.to_store_entries` followed by a diff-job
    write. Assumes a green store (the loader contract): a malformed entry raises.
    """
    return from_store_entries(load_store_entries(root))
