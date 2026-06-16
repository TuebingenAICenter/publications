"""Store invariant checks (CI gate).

The deterministic, secret-free, pure-Python backstop that verifies the committed
store is in a "good state." It runs after the normalizer and before the commit-back
(see ``.github/workflows/normalize.yml``); any failure prints ``::error::``
annotations and exits non-zero so a broken tree is never pushed to ``main``. It is
the "fail loudly, don't auto-heal" half of the design — we surface a problem for a
human rather than silently reshaping committed content.

Each check maps to a *Normalization invariant* in
``planning/github-bibtex-store-plan.md``:

- **Bijection by existence (#1):** every ``entries/<year>/<stem>.bib`` has exactly
  one ``meta/<year>/<stem>.json`` at the same year + stem, and vice versa. The
  *content* of a sidecar may be empty (``{}``), but the file must exist.
- **Sidecar shape (#4):** every ``meta/**/*.json`` validates against
  ``schema/sidecar.schema.json`` — a bare ``{}`` or any combination of the two
  optional halves ``zotero`` / ``custom``, and nothing else at top level. A flat /
  legacy sidecar is **rejected, not migrated**.
- **Canonical bib form (#3):** every ``entries/**/*.bib`` is at the fixpoint of the
  formatter — ``to_bibtex(from_bibtex(text)) == text`` — so re-normalizing is a
  no-op (this is the per-file half of idempotency #8).
- **Year-shard correctness (#2):** an entry lives in ``entries/<year>/`` where
  ``<year>`` is its own ``year`` field (or ``undated``), not whatever directory a
  contributor dropped it in.
- **Compile-ability / global citekey uniqueness (#10):** the full set parses and
  compiles to a single valid ``all.bib`` with no duplicate citekeys across files.
- **Filename == citekey (#12):** every ``entries/<year>/<stem>.bib`` has ``stem``
  equal to its own citekey. The citekey is the entry's single identity and the path
  is derived from it; this is what lets the filesystem enforce citekey uniqueness as
  an add/add conflict. No naming *scheme* is enforced on the key itself — only that
  the filename matches it.

Semantic deduplication (invariant #11) is deliberately **not** a gate — it is the
out-of-band, human-reviewed sweep (see the plan's *Deduplication* section).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from zotero_rdf import BibtexParseError, from_bibtex, to_bibtex

from normalize_store import _CITEKEY_RE, _year_of
from sidecar_schema import validation_error


def _bib_paths(repo_root: Path) -> list[Path]:
    return sorted((Path(repo_root) / "entries").rglob("*.bib"))


def bijection_orphans(repo_root: Path) -> tuple[list[str], list[str]]:
    """#1 — return ``(bib_without_meta, meta_without_bib)`` as ``<year>/<stem>`` keys."""
    repo_root = Path(repo_root)
    entries_dir, meta_dir = repo_root / "entries", repo_root / "meta"
    entries = {
        str(p.relative_to(entries_dir).with_suffix(""))
        for p in entries_dir.rglob("*.bib")
    }
    metas = {
        str(p.relative_to(meta_dir).with_suffix(""))
        for p in meta_dir.rglob("*.json")
    }
    return sorted(entries - metas), sorted(metas - entries)


def sidecar_shape_errors(repo_root: Path) -> list[str]:
    """#4 — one message per ``meta/**`` sidecar that fails ``schema/sidecar.schema.json``."""
    repo_root = Path(repo_root)
    meta_dir = repo_root / "meta"
    errors: list[str] = []
    for path in sorted(meta_dir.rglob("*.json")):
        rel = path.relative_to(repo_root)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{rel} is not valid JSON ({exc})")
            continue
        message = validation_error(data, str(rel))
        if message is not None:
            errors.append(message)
    return errors


def canonical_format_errors(repo_root: Path) -> list[str]:
    """#3 + #2 — every ``.bib`` is canonical and sits in its own year shard.

    Re-derives the canonical form independently of the normalizer (parse → re-emit,
    no sidecar — the ``zotero`` half holds only non-bib fields, so it never affects
    bib output). If the re-emitted text differs from what is on disk, the normalizer
    would rewrite the file, i.e. the store is not at its fixpoint. The directory the
    file lives in must equal its own ``year`` field — sharding is driven by the data,
    not the path a contributor chose.
    """
    repo_root = Path(repo_root)
    errors: list[str] = []
    for path in _bib_paths(repo_root):
        rel = path.relative_to(repo_root)
        text = path.read_text(encoding="utf-8")
        try:
            items = from_bibtex(text)
        except BibtexParseError as exc:
            errors.append(f"{rel} does not parse as BibTeX ({exc})")
            continue
        if len(items) != 1:
            errors.append(
                f"{rel} holds {len(items)} entries — the store is one entry per file"
            )
            continue
        bib, _ = to_bibtex(items)
        canonical = bib.strip() + "\n"
        if canonical != text:
            errors.append(
                f"{rel} is not in canonical form (the normalizer would rewrite it)"
            )
        shard, year = path.parent.name, _year_of(text)
        if shard != year:
            errors.append(
                f"{rel} is in entries/{shard}/ but its year field is {year} "
                f"(year-shard mismatch — move it to entries/{year}/)"
            )
    return errors


def citekey_match_errors(repo_root: Path) -> list[str]:
    """#12 — every entry's filename stem equals its citekey.

    The citekey is the entry's single identity; the path is derived from it, so
    ``entries/<year>/<citekey>.bib`` is a pure function of the key (index-free
    lookup) and two adds of the same key collide on the same path (filesystem-
    enforced uniqueness). We do **not** enforce any naming *scheme* on the citekey —
    only that the filename matches whatever the key is. A keyless / unparseable file
    is reported by the compile check, not here.
    """
    repo_root = Path(repo_root)
    errors: list[str] = []
    for path in _bib_paths(repo_root):
        rel = path.relative_to(repo_root)
        m = _CITEKEY_RE.search(path.read_text(encoding="utf-8"))
        if m is None:
            continue
        citekey = m.group(1)
        if path.stem != citekey:
            errors.append(
                f"{rel}: filename stem '{path.stem}' != citekey '{citekey}' "
                f"(the filename must equal the citekey — rename to '{citekey}.bib')"
            )
    return errors


def compile_all_bib(repo_root: Path) -> tuple[str, list[str]]:
    """#10 — compile every entry into one deterministic ``all.bib``.

    Returns ``(all_bib_text, errors)``. Entries are sorted by citekey so the output
    is reproducible. ``errors`` lists everything that would break a valid ``all.bib``:
    a file that does not parse, a missing citekey, or a citekey used by more than one
    file (cross-entry duplicate — the global-uniqueness failure the per-file path
    can't see). The compiled text is a build artifact, not committed.
    """
    repo_root = Path(repo_root)
    errors: list[str] = []
    by_key: dict[str, tuple[str, object]] = {}  # citekey -> (rel, item)
    for path in _bib_paths(repo_root):
        rel = str(path.relative_to(repo_root))
        text = path.read_text(encoding="utf-8")
        m = _CITEKEY_RE.search(text)
        if not m:
            errors.append(f"{rel} has no citekey")
            continue
        citekey = m.group(1)
        try:
            items = from_bibtex(text)
        except BibtexParseError as exc:
            errors.append(f"{rel} does not parse as BibTeX ({exc})")
            continue
        if citekey in by_key:
            errors.append(
                f"duplicate citekey {citekey!r}: {by_key[citekey][0]} and {rel} "
                f"(global citekey uniqueness broken)"
            )
            continue
        by_key[citekey] = (rel, items[0])

    ordered = [by_key[k][1] for k in sorted(by_key)]
    all_bib, _ = to_bibtex(ordered) if ordered else ("", {})
    # Re-parse the concatenation as a final compile-ability assertion: even with
    # unique keys per file, a corrupt join would surface here rather than downstream.
    if ordered:
        try:
            reparsed = from_bibtex(all_bib)
        except BibtexParseError as exc:
            errors.append(f"compiled all.bib does not parse ({exc})")
        else:
            if len(reparsed) != len(ordered):
                errors.append(
                    f"compiled all.bib has {len(reparsed)} entries, expected {len(ordered)}"
                )
    return all_bib, errors


def compile_errors(repo_root: Path) -> list[str]:
    """#10 — the error list from :func:`compile_all_bib` (drops the compiled text)."""
    return compile_all_bib(repo_root)[1]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Check store invariants (bijection, sidecar shape, canonical form, compile)."
    )
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    ap.add_argument(
        "--write-all-bib",
        type=Path,
        default=None,
        help="also write the compiled all.bib to this path (build artifact; not a gate)",
    )
    args = ap.parse_args()

    bib_without_meta, meta_without_bib = bijection_orphans(args.root)
    for stem in bib_without_meta:
        print(f"::error::entries/{stem}.bib has no sidecar meta/{stem}.json (bijection broken)")
    for stem in meta_without_bib:
        print(
            f"::error::meta/{stem}.json has no entry entries/{stem}.bib — orphan sidecar "
            f"(did a .bib move without its sidecar? move the .json to match, or revert the "
            f"move and edit the entry in place so the bot relocates both)"
        )

    shape_errors = sidecar_shape_errors(args.root)
    format_errors = canonical_format_errors(args.root)
    citekey_errors = citekey_match_errors(args.root)
    all_bib, comp_errors = compile_all_bib(args.root)
    for msg in (*shape_errors, *format_errors, *citekey_errors, *comp_errors):
        print(f"::error::{msg}")

    failed = bool(
        bib_without_meta
        or meta_without_bib
        or shape_errors
        or format_errors
        or citekey_errors
        or comp_errors
    )
    if failed:
        print(
            f"FAIL: {len(bib_without_meta)} entry orphan(s), "
            f"{len(meta_without_bib)} sidecar orphan(s), "
            f"{len(shape_errors)} malformed sidecar(s), "
            f"{len(format_errors)} non-canonical/misplaced bib(s), "
            f"{len(citekey_errors)} filename≠citekey, "
            f"{len(comp_errors)} compile error(s)"
        )
        sys.exit(1)

    if args.write_all_bib is not None:
        args.write_all_bib.write_text(all_bib, encoding="utf-8")
        print(f"wrote compiled all.bib → {args.write_all_bib}")
    print(
        "OK: bijection holds, every sidecar validates, bibs are canonical, "
        "filenames match citekeys, store compiles"
    )


if __name__ == "__main__":
    main()
