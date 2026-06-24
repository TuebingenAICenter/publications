"""Shared emit core: a :class:`StoreEntry` → its canonical on-disk ``(path, content)`` pair.

The in-memory counterpart of one :func:`publication_store.diff_job.normalize_changed`
write, factored out so every *producer* that places entries into the store layout
agrees on canonical form and renders the sidecar identically:

- the bulk RDF build (``scratchpad/.../build_store_from_rdf.py``), which writes the
  pair to local disk, and
- the per-item PR publisher (:mod:`publication_store.publish`), which commits the
  same bytes as a git tree via the GitHub Git Data API — no working tree.

Keeping the canonicalization (S4 fixpoint) and the sidecar serialization in one
place is what makes a pair built here one that CI ``pubstore-normalize`` accepts
**unchanged** (normalize stays a no-op): the bytes are byte-identical to what the
diff job would write. No git, no argparse, no disk — the caller decides where the
bytes land.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from . import entry

if TYPE_CHECKING:  # only for the type hint — avoids importing the bridge at runtime
    from .zotero_bridge import StoreEntry

#: A title that needs more than one ``from_bibtex → to_bibtex`` pass to reach the
#: fixpoint is pathological but real (e.g. ``\textasciicircum``); a couple of passes
#: always suffice. Exceeding this means a genuine non-convergence (a tooling bug).
_MAX_PASSES = 5


class EmittedPair(NamedTuple):
    """One store record's two files, in memory: ``.bib`` + ``.json`` paths + contents.

    ``bib_path`` / ``meta_path`` are repo-root-relative (``entries/<year>/<key>.bib``,
    ``meta/<year>/<key>.json``); the texts are exactly what the diff job would write.
    """

    bib_path: Path
    bib_text: str
    meta_path: Path
    meta_text: str


def render_sidecar(zotero: dict, custom: dict) -> str:
    """The sidecar JSON exactly as the diff job writes it — sorted, 2-space, trailing ``\\n``.

    The single source of this serialization: every producer (diff job, bulk build,
    PR publisher) must emit byte-identical sidecars so the store's idempotency holds.
    """
    return (
        json.dumps(
            {"zotero": zotero, "custom": custom},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def canonicalize(bib: str, citekey: str, zotero_half: dict) -> tuple[str, str, dict]:
    """Iterate ``from_bibtex → to_bibtex`` to the S4 fixpoint; return ``(year, bib, zotero)``.

    The checker recomputes canonical form without a sidecar, so the ``zotero`` half
    (fields BibTeX can't carry) is overlaid each pass only to keep the spill we store
    consistent with the text — it never changes the text. The citekey is explicit so
    it is preserved unchanged across passes. Raises ``ValueError`` if it does not
    converge within :data:`_MAX_PASSES`, or on a parse error from the underlying
    round-trip.
    """
    sidecar_map = {citekey: zotero_half} if zotero_half else None
    for _ in range(_MAX_PASSES):
        ((_, year, new_bib, zotero),) = entry.canonical_entries(bib, sidecar_map)
        if new_bib == bib:
            return year, bib, zotero
        bib = new_bib
    raise ValueError(
        f"{citekey}: canonicalization did not converge in {_MAX_PASSES} passes"
    )


def emit_pair(store_entry: StoreEntry) -> EmittedPair:
    """A :class:`StoreEntry` → its canonical ``(bib, meta)`` relpaths + contents, in memory.

    Canonicalize the bib to the S4 fixpoint (overlaying the entry's ``zotero`` half),
    derive its placement from the resulting year + citekey, and render the sidecar
    byte-for-byte the way the diff job does — the stored ``zotero`` half is the
    *canonicalized* spill, not ``store_entry.sidecar["zotero"]`` verbatim. The caller
    writes the two files wherever it likes (local disk, a git tree blob).
    """
    custom = store_entry.sidecar.get("custom", {})
    zotero_half = store_entry.sidecar.get("zotero", {})
    year, bib_text, zotero_spill = canonicalize(
        store_entry.bib, store_entry.citekey, zotero_half
    )
    bib_rel, meta_rel = entry.derive_path(year, store_entry.citekey)
    return EmittedPair(bib_rel, bib_text, meta_rel, render_sidecar(zotero_spill, custom))
