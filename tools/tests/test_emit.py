"""Tests for the shared emit core (``publication_store.emit``).

The emit core is what every *producer* (bulk RDF build, per-item PR publisher) uses
to turn a :class:`StoreEntry` into its canonical ``(path, content)`` pair without
going through the diff job's changed-file flow. The contract that matters: a pair it
builds is byte-identical to what the diff job would write — same canonical bib, same
sidecar JSON — so CI ``pubstore-normalize`` stays a no-op on a published pair.

Manual developer harness, **not** wired into CI (consistent with the rest of
``tools/tests/``).
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_store, run_check
from publication_store import emit, entry
from publication_store.zotero_bridge import to_store_entries
from zotero_rdf import JournalArticle, Tag, ZoteroCollection


def _entry(title="Predictive Coding", date="2025", groups=("bethge",), mentions=True):
    item = JournalArticle(
        title=title,
        publicationTitle="Nature",
        date=date,
        creators=[],
        tags=[Tag(tag="ml")] + ([Tag(tag="mentionsAICenter")] if mentions else []),
    )
    collections = [ZoteroCollection(name=g) for g in groups]
    for c in collections:
        c.add(item)
    (store_entry,) = to_store_entries([item], collections)
    return store_entry


def test_emit_pair_derives_canonical_paths_and_texts():
    se = _entry()
    pair = emit.emit_pair(se)

    # Placement is the derived (year, citekey) path.
    bib_rel, meta_rel = entry.derive_path("2025", se.citekey)
    assert pair.bib_path == bib_rel
    assert pair.meta_path == meta_rel
    # The bib text is at the formatter fixpoint (canonical_entry would not rewrite it).
    citekey, _, canonical, _ = entry.canonical_entry(pair.bib_text)
    assert canonical == pair.bib_text
    assert citekey == se.citekey


def test_emitted_sidecar_is_byte_identical_to_the_diff_job(tmp_path: Path):
    # Build the same entry two ways: emit_pair (the producer path) and make_store
    # (which writes the sidecar exactly as the diff job does). The .json must match
    # byte-for-byte, or CI normalize would rewrite a published entry.
    se = _entry()
    pair = emit.emit_pair(se)

    root = make_store(
        tmp_path,
        [{"bib": se.bib, "zotero": se.sidecar["zotero"], "custom": se.sidecar["custom"]}],
    )
    diff_job_meta = (root / pair.meta_path).read_text(encoding="utf-8")
    diff_job_bib = (root / pair.bib_path).read_text(encoding="utf-8")
    assert pair.meta_text == diff_job_meta
    assert pair.bib_text == diff_job_bib


def test_emitted_pair_passes_the_store_checker(tmp_path: Path):
    # Write a pair built purely by emit_pair into a fresh store; the full-store
    # checker (S1–S5) must accept it with no findings.
    se = _entry()
    pair = emit.emit_pair(se)
    from conftest import install_schema

    install_schema(tmp_path)
    for rel, text in ((pair.bib_path, pair.bib_text), (pair.meta_path, pair.meta_text)):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
    assert run_check(tmp_path) == []


def test_canonicalize_preserves_explicit_citekey_and_year():
    se = _entry(date="2019")
    year, bib_text, _ = emit.canonicalize(se.bib, se.citekey, se.sidecar["zotero"])
    assert year == "2019"
    assert entry.citekey_of(bib_text) == se.citekey
