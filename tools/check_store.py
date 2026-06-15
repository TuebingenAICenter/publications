"""Store invariant checks (CI gate).

Enforces two invariants on the committed store:

- **Bijection by existence (#1):** every ``entries/<year>/<stem>.bib`` has exactly
  one ``meta/<year>/<stem>.json`` at the same year + stem, and vice versa. The
  *content* of a sidecar may be empty (``{}``), but the file must exist.
- **Sidecar shape (#4):** every ``meta/**/*.json`` is valid JSON with exactly the
  two top-level keys ``zotero`` and ``custom``, both objects (possibly empty). A
  flat / legacy sidecar is **rejected, not migrated** — content can be omitted
  (an empty ``{}`` half is fine), but anything actually committed must already be
  in the canonical wrapped shape.

Run after the normalizer and before committing: a broken store fails the job
(non-zero exit + ``::error::`` annotations) so it is never committed. This is the
deliberate "fail loudly" backstop — we do **not** try to auto-heal a contributor
who moved a ``.bib`` without its sidecar, nor silently rewrite a malformed
sidecar; we surface it for a human to fix.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def bijection_orphans(repo_root: Path) -> tuple[list[str], list[str]]:
    """Return ``(bib_without_meta, meta_without_bib)`` as ``<year>/<stem>`` keys."""
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
    """Return one message per ``meta/**`` sidecar not in canonical ``{zotero, custom}`` shape."""
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
        if not isinstance(data, dict) or set(data) != {"zotero", "custom"}:
            found = sorted(data) if isinstance(data, dict) else type(data).__name__
            errors.append(
                f"{rel} must have exactly the top-level keys 'zotero' and 'custom' "
                f"(found: {found or 'empty object'}) — a flat/legacy sidecar is rejected, "
                f"not migrated; wrap it as {{\"zotero\": {{...}}, \"custom\": {{...}}}}"
            )
            continue
        for key in ("zotero", "custom"):
            if not isinstance(data[key], dict):
                errors.append(f"{rel}: '{key}' must be an object (possibly empty)")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Check store invariants (bijection + sidecar shape).")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
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
    for msg in shape_errors:
        print(f"::error::{msg}")

    if bib_without_meta or meta_without_bib or shape_errors:
        print(
            f"FAIL: {len(bib_without_meta)} entry orphan(s), "
            f"{len(meta_without_bib)} sidecar orphan(s), "
            f"{len(shape_errors)} malformed sidecar(s)"
        )
        sys.exit(1)
    print("OK: bijection holds and every sidecar is canonical {zotero, custom}")


if __name__ == "__main__":
    main()
