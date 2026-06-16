"""Normalize publication contributions into the sharded store layout.

This is the PR-time normalizer: given the set of ``.bib`` files changed in a
pull request (located anywhere in the repo), it rewrites each entry into the
store layout

    entries/<year>/<stem>.bib    one canonical BibTeX entry per file
    meta/<year>/<stem>.json      matching sidecar, {"zotero": ..., "custom": ...}

and removes the raw source files (a paste dropped anywhere, or the legacy
monolith). The *same* path serves the initial big-data PR and every future
one-entry PR — there is no separate migration tool.

Naming policy (the filename scheme is a convenience, not an identity):

- **single-item file → keep its filename.** Contributors may name files however
  they like; we only canonicalize the bib, place it in ``entries/<year>/``, and
  keep a matching ``meta/<year>/<stem>.json``.
- **multi-item file → split, name each part by the citekey scheme** (the
  fallback namer) and delete the original multi-item file.

Hard requirements: the year-sharded directory structure and a strict 1:1
``.bib`` ↔ ``.json`` mapping (same stem). Filenames are otherwise free, so a
filename may differ from the entry's internal citekey.

Other mechanics:

- **relocation carries data** — an edit that changes an entry's year moves the
  pair into the new year directory and transfers the existing ``custom`` report
  data, deleting the old pair (no orphan, no data loss).
- **custom-half preservation** — re-normalizing keeps any existing ``custom``
  report data and only refreshes the ``zotero`` half.
- **collision seam** — writing onto a *different* pre-existing entry is the
  deferred-dedup case; surfaced as a warning, never a silent overwrite.

Idempotent: a canonical store re-normalizes to itself (no diff, no commit),
which is also the Action's loop guard.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from zotero_rdf import from_bibtex, to_bibtex

from sidecar_schema import split_sidecar

_CITEKEY_RE = re.compile(r"^@\w+\{([^,]+),", re.MULTILINE)
_YEAR_RE = re.compile(r"^\s*year\s*=\s*\{?(\d{4})", re.MULTILINE)
_UNDATED = "undated"


def _split_entries(bib_string: str) -> list[tuple[str, str]]:
    """Split ``to_bibtex`` output into ``(citekey, entry_text)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for chunk in bib_string.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _CITEKEY_RE.search(chunk)
        if not m:
            raise ValueError(f"could not find citekey in entry:\n{chunk[:200]}")
        pairs.append((m.group(1), chunk + "\n"))
    return pairs


def _year_of(entry_text: str) -> str:
    m = _YEAR_RE.search(entry_text)
    return m.group(1) if m else _UNDATED


def _load_store_sidecar(meta_path: Path) -> tuple[dict, dict]:
    """Read a stored sidecar's ``(zotero, custom)`` halves, failing loudly on bad shape.

    Validates against ``schema/sidecar.schema.json`` (see ``sidecar_schema``): a
    bare ``{}`` or a missing half defaults to ``{}``, but a flat / legacy sidecar is
    **rejected, not migrated** — silently reading it as ``.get("zotero", {})`` would
    drop the whole overlay (invariant #4 / the ``test/pr3`` data-loss path). The fix
    is a human one (wrap it, or empty it), never a silent rewrite.
    """
    if not meta_path.exists():
        return {}, {}
    data = json.loads(meta_path.read_text(encoding="utf-8"))  # JSONDecodeError propagates → fail
    return split_sidecar(data, str(meta_path))


def _in_store_meta(repo_root: Path, bib_path: Path) -> Path | None:
    """The ``meta/`` path for a ``.bib`` that already lives under ``entries/``."""
    try:
        rel = bib_path.resolve().relative_to((repo_root / "entries").resolve())
    except ValueError:
        return None
    return repo_root / "meta" / rel.with_suffix(".json")


def _plan_file(repo_root: Path, bib_path: Path):
    """Plan the targets for one changed ``.bib``.

    Returns ``(targets, extra_sources, carried_custom)`` where each target is
    ``(year, stem, citekey, entry_text, zotero)``. ``carried_custom`` is the
    existing ``custom`` half to carry when a single in-store entry relocates;
    ``extra_sources`` are sibling raw files (a drop's ``<stem>.json``) to delete.
    """
    text = bib_path.read_text(encoding="utf-8")
    in_store_meta = _in_store_meta(repo_root, bib_path)

    # Sidecar for from_bibtex: in-store entries take the zotero half from their
    # stored meta (keyed by the entry's *internal* citekey, which may differ
    # from the filename); raw drops take a sibling {citekey: sidecar} map.
    extra_sources: list[Path] = []
    if in_store_meta is not None:
        zotero, carried_custom = _load_store_sidecar(in_store_meta)
        ck = (_CITEKEY_RE.search(text) or [None, None])[1] if in_store_meta.exists() else None
        sidecar_map = {ck: zotero} if ck else None
    else:
        sibling = bib_path.with_suffix(".json")
        sidecar_map = json.loads(sibling.read_text(encoding="utf-8")) if sibling.exists() else None
        if sibling.exists():
            extra_sources.append(sibling)
        carried_custom = {}

    items = from_bibtex(text, sidecar=sidecar_map)
    bib_string, spill = to_bibtex(items)
    entries = _split_entries(bib_string)
    single = len(entries) == 1

    targets = []
    for citekey, entry_text in entries:
        year = _year_of(entry_text)
        stem = bib_path.stem if single else citekey  # keep filename if single, else citekey
        targets.append((year, stem, citekey, entry_text, spill.get(citekey, {})))
    return targets, extra_sources, (carried_custom if single else {})


def normalize_changed(repo_root: Path, changed_bibs: list[Path]):
    """Normalize the ``.bib`` files changed in a PR; relocate + delete raw sources.

    Returns ``(written, removed, warnings)``.
    """
    repo_root = Path(repo_root)
    changed_set = {Path(b).resolve() for b in changed_bibs}

    plans = []  # (source_bib, targets, extra_sources, carried_custom)
    for bib in changed_bibs:
        bib = Path(bib)
        if not bib.exists():  # deleted in the PR — nothing to parse
            continue
        targets, extra, carried = _plan_file(repo_root, bib)
        plans.append((bib, targets, extra, carried))

    target_bibs = {
        (repo_root / "entries" / year / f"{stem}.bib").resolve()
        for _, targets, _, _ in plans
        for (year, stem, _, _, _) in targets
    }

    written: list[Path] = []
    warnings: list[str] = []
    for _, targets, _, carried in plans:
        for year, stem, citekey, entry_text, zotero in targets:
            bib_path = repo_root / "entries" / year / f"{stem}.bib"
            meta_path = repo_root / "meta" / year / f"{stem}.json"

            if (
                bib_path.exists()
                and bib_path.resolve() not in changed_set
                and bib_path.read_text(encoding="utf-8") != entry_text
            ):
                warnings.append(
                    f"{bib_path} already exists with different content "
                    f"(possible duplicate of {citekey} — needs dedup/human review)"
                )

            _, existing_custom = _load_store_sidecar(meta_path)
            custom = existing_custom or carried
            bib_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            bib_path.write_text(entry_text, encoding="utf-8")
            meta_path.write_text(
                json.dumps({"zotero": zotero, "custom": custom}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.extend([bib_path, meta_path])

    # Delete raw drops and relocation leftovers (source bib no longer a target).
    removed: list[Path] = []
    for bib, _, extra, _ in plans:
        if bib.resolve() not in target_bibs and bib.exists():
            bib.unlink()
            removed.append(bib)
            old_meta = _in_store_meta(repo_root, bib)  # relocation: drop the old sidecar too
            if old_meta is not None and old_meta.exists():
                old_meta.unlink()
                removed.append(old_meta)
        for e in extra:
            if e.exists():
                e.unlink()
                removed.append(e)
    return written, removed, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize changed .bib files into the store layout.")
    ap.add_argument("paths", nargs="+", type=Path, help="changed .bib files (from git diff)")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    args = ap.parse_args()
    try:
        written, removed, warnings = normalize_changed(args.root, args.paths)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error::normalize aborted (malformed input, nothing written): {exc}")
        sys.exit(1)
    print(f"wrote {len(written)} files ({len(written) // 2} entries); removed {len(removed)} raw source(s)")
    for w in warnings:
        print(f"  ::warning:: {w}")


if __name__ == "__main__":
    main()
