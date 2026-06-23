"""Add-on — compile the per-entry store into consumer-facing joined views.

A **separable** add-on (``pubstore-compile``): it reads a *valid* store and emits
build artifacts, never committed, always reproducible from ``entries/**`` +
``meta/**``. It is **not a gate** — it assumes S1–S5 already hold (run it on a
green ``main``), warns + skips anything that won't parse, and never fails the
merge. It reasons about nothing: no dedup, no matching, no enrichment.

Four artifacts, each a pure ``(repo_root) -> (payload, warnings)`` function with no
git, no argparse, no I/O of its own (``main`` writes them out):

- :func:`compile_all_bib` — every entry in one deterministic, citekey-sorted
  ``all.bib``. (Relocated here from ``checker.py`` so the S1–S5 gate stays pure.)
- :func:`compile_group_bibs` — one ``<slug>.bib`` per PI group, holding the entries
  whose sidecar ``custom.groups`` contains that slug (a co-owned paper appears in
  both).
- :func:`compile_meta_json` — a lossless ``{citekey: <verbatim sidecar>}`` join of
  every ``meta/**`` sidecar. No bib parsing, no field selection: a single-file
  mirror of the overlay data.
- :func:`compile_rdf` — the **full** library in one Zotero-importable ``library.rdf``:
  every entry's lossless ``zotero`` half plus ``custom.groups`` rebuilt into Zotero
  collections and the ``mentionsAICenter`` tag re-added (via the
  :mod:`publication_store.store` / :mod:`publication_store.zotero_bridge` round-trip).
  Unlike ``all.bib`` it carries group membership + tags, so it re-imports into the
  Zotero GUI as the whole library.

Deps stay ``zotero-rdf`` + ``jsonschema`` (no ``publib``); the slug vocabulary is
**not** enforced — group bibs are emitted for whatever slugs actually appear in
``custom.groups`` (the agents own the vocabulary, per the plan).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from zotero_rdf import BibtexParseError, export_to_rdf, from_bibtex, to_bibtex

from . import entry, sidecar
from .store import bib_paths, meta_path_for, meta_paths
from .zotero_bridge import StoreEntry, from_store_entries

# The artifact kinds, in emit order. Used both as the ``--only`` vocabulary and as
# the default (emit all of them).
ARTIFACT_KINDS = ("all-bib", "group-bibs", "meta-json", "rdf")


def compile_all_bib(repo_root: Path) -> tuple[str, list[str]]:
    """Compile every entry into one deterministic ``all.bib`` (not a gate).

    Returns ``(all_bib_text, warnings)``. Entries are sorted by citekey for a
    reproducible artifact. ``warnings`` lists anything that would mar the join (a
    file that does not parse, or holds more than one entry); on a store that
    already passed S1–S5 it is empty. The compiled text is a build artifact, never
    committed.
    """
    repo_root = Path(repo_root)
    warnings: list[str] = []
    by_key: dict[str, object] = {}
    for path in bib_paths(repo_root):
        rel = str(path.relative_to(repo_root))
        text = path.read_text(encoding="utf-8")
        try:
            items = from_bibtex(text)
        except BibtexParseError as exc:
            warnings.append(f"{rel} does not parse as BibTeX ({exc})")
            continue
        if len(items) != 1:
            warnings.append(f"{rel} holds {len(items)} entries — skipped")
            continue
        key = entry.citekey_of(text) or path.stem
        by_key[key] = items[0]
    ordered = [by_key[k] for k in sorted(by_key)]
    all_bib, _ = to_bibtex(ordered) if ordered else ("", {})
    return all_bib, warnings


def compile_group_bibs(repo_root: Path) -> tuple[dict[str, str], list[str]]:
    """Compile one citekey-sorted ``<slug>.bib`` per PI group (not a gate).

    Returns ``({slug: bib_text}, warnings)``. For each entry it reads the sidecar's
    ``custom.groups`` and buckets the (lossless round-tripped) item under every slug
    it lists; an entry with no ``custom.groups`` (or ``custom: {}``) lands in no
    group bib (it is still in ``all.bib``). The ``zotero`` half is fed back into
    ``from_bibtex`` so each emitted item is the full lossless ``ZoteroItem``. A file
    that won't parse, or a sidecar that won't read/validate, is warned + skipped —
    won't happen on a green store. The slug vocabulary is not enforced (the agents
    own it): a bib is emitted for whatever slugs appear in the data.
    """
    repo_root = Path(repo_root)
    warnings: list[str] = []
    # slug -> {citekey: item}; dict keeps the last item per key (dedup is the
    # checker's job — on a green store a citekey is globally unique anyway).
    buckets: dict[str, dict[str, object]] = {}
    for path in bib_paths(repo_root):
        rel = str(path.relative_to(repo_root))
        meta_path = meta_path_for(path, repo_root)
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"{meta_path.relative_to(repo_root)} is not readable JSON ({exc}) — skipped")
            continue
        try:
            zotero_half, custom_half = sidecar.split_sidecar(
                data, str(meta_path.relative_to(repo_root)), repo_root
            )
        except ValueError as exc:
            warnings.append(f"{exc} — skipped")
            continue
        groups = custom_half.get("groups") or []
        if not groups:
            continue  # no group ownership — only in all.bib

        text = path.read_text(encoding="utf-8")
        citekey = entry.citekey_of(text) or path.stem
        sidecar_map = {citekey: zotero_half} if zotero_half else None
        try:
            items = from_bibtex(text, sidecar=sidecar_map)
        except BibtexParseError as exc:
            warnings.append(f"{rel} does not parse as BibTeX ({exc}) — skipped")
            continue
        if len(items) != 1:
            warnings.append(f"{rel} holds {len(items)} entries — skipped")
            continue
        for slug in groups:
            buckets.setdefault(slug, {})[citekey] = items[0]

    group_bibs: dict[str, str] = {}
    for slug in sorted(buckets):
        ordered = [buckets[slug][k] for k in sorted(buckets[slug])]
        bib_text, _ = to_bibtex(ordered) if ordered else ("", {})
        group_bibs[slug] = bib_text
    return group_bibs, warnings


def compile_meta_json(repo_root: Path) -> tuple[dict[str, dict], list[str]]:
    """Compile a lossless ``{citekey: <verbatim sidecar>}`` join of every sidecar.

    Returns ``({citekey: sidecar}, warnings)``. **No bib parsing** — walks
    ``meta/**/*.json``, keys each by its stem (== citekey by S1), and sets the value
    to the verbatim parsed sidecar (``{"zotero": …, "custom": …}``, exactly as
    stored). Its shape *is* the sidecar schema, one level up keyed by citekey — a
    pure mirror of ``meta/**``, the heavy ``zotero`` half included by design. A
    sidecar that isn't valid JSON / fails the schema is warned + skipped (won't
    happen on a green store — S3/S4 already guarantee it).
    """
    repo_root = Path(repo_root)
    warnings: list[str] = []
    by_key: dict[str, dict] = {}
    for meta_path in meta_paths(repo_root):
        rel = str(meta_path.relative_to(repo_root))
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"{rel} is not readable JSON ({exc}) — skipped")
            continue
        message = sidecar.validation_error(data, rel, repo_root)
        if message is not None:
            warnings.append(f"{message} — skipped")
            continue
        by_key[meta_path.stem] = data
    return by_key, warnings


def _rdf_bytes(items, collections) -> bytes:
    """Serialize a library to Zotero RDF/XML bytes.

    ``zotero_rdf.export_to_rdf`` only writes to a path (it postprocesses to bytes for
    the ``rdf:resource`` import workaround), so we round-trip through a temp file and
    read the bytes back — keeping :func:`compile_rdf` a pure ``(root) -> (bytes,
    warnings)`` like its siblings.
    """
    with tempfile.NamedTemporaryFile(suffix=".rdf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        export_to_rdf(items, tmp_path, collections=collections)
        return Path(tmp_path).read_bytes()
    finally:
        os.unlink(tmp_path)


def compile_rdf(repo_root: Path) -> tuple[bytes, list[str]]:
    """Compile the whole store into one Zotero-importable RDF library (not a gate).

    Returns ``(rdf_bytes, warnings)``. Unlike ``all.bib`` (items only) and the group
    bibs (split per slug), this is the **full lossless library in one file**: every
    entry's ``zotero`` half overlaid via ``from_bibtex``, ``custom.groups`` rebuilt
    into Zotero collections, and the ``mentionsAICenter`` tag re-added — exactly the
    round-trip :mod:`publication_store.zotero_bridge` /
    :mod:`publication_store.store` define, so the file re-imports into the Zotero GUI
    with its groups + tags intact. A file that won't parse, or a sidecar that won't
    read/validate, is warned + skipped (won't happen on a green store). Each surviving
    ``.bib`` is pre-validated to parse (the tolerance filter) so the single
    :func:`from_store_entries` over the survivors — which dedups collections across the
    batch — never raises.
    """
    repo_root = Path(repo_root)
    warnings: list[str] = []
    entries: list[StoreEntry] = []
    for path in bib_paths(repo_root):
        rel = str(path.relative_to(repo_root))
        meta_path = meta_path_for(path, repo_root)
        meta_rel = str(meta_path.relative_to(repo_root))
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"{meta_rel} is not readable JSON ({exc}) — skipped")
            continue
        message = sidecar.validation_error(data, meta_rel, repo_root)
        if message is not None:
            warnings.append(f"{message} — skipped")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            items = from_bibtex(text)
        except BibtexParseError as exc:
            warnings.append(f"{rel} does not parse as BibTeX ({exc}) — skipped")
            continue
        if len(items) != 1:
            warnings.append(f"{rel} holds {len(items)} entries — skipped")
            continue
        entries.append(StoreEntry(citekey=path.stem, bib=text, sidecar=data))

    items, collections = from_store_entries(entries)
    return _rdf_bytes(items, collections), warnings


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compile the publication store into joined build artifacts "
        "(all.bib / <group>.bib / meta.json / library.rdf). Read-only, never a gate."
    )
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    ap.add_argument(
        "--out", type=Path, default=Path("build"), help="output dir (default: build/, created if absent)"
    )
    ap.add_argument(
        "--only",
        action="append",
        choices=ARTIFACT_KINDS,
        default=None,
        help="emit only this artifact (repeatable); default emits all three",
    )
    args = ap.parse_args()

    root: Path = args.root
    if not (root / "entries").is_dir():
        print(f"::error::{root} has no entries/ — not a publication store")
        sys.exit(1)

    kinds = args.only if args.only else list(ARTIFACT_KINDS)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if "all-bib" in kinds:
        all_bib, w = compile_all_bib(root)
        warnings.extend(w)
        (out / "all.bib").write_text(all_bib, encoding="utf-8")
        print(f"wrote {out / 'all.bib'}")

    if "group-bibs" in kinds:
        group_bibs, w = compile_group_bibs(root)
        warnings.extend(w)
        groups_dir = out / "groups"
        groups_dir.mkdir(parents=True, exist_ok=True)
        for slug in sorted(group_bibs):
            (groups_dir / f"{slug}.bib").write_text(group_bibs[slug], encoding="utf-8")
        print(f"wrote {len(group_bibs)} group bib(s) → {groups_dir}")

    if "meta-json" in kinds:
        meta, w = compile_meta_json(root)
        warnings.extend(w)
        (out / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {out / 'meta.json'} ({len(meta)} entries)")

    if "rdf" in kinds:
        rdf, w = compile_rdf(root)
        warnings.extend(w)
        (out / "library.rdf").write_bytes(rdf)
        print(f"wrote {out / 'library.rdf'}")

    for warning in warnings:
        print(f"::warning::{warning}")
    # Not a gate: warnings never fail the run. Exit 0 even with skipped files.


if __name__ == "__main__":
    main()
