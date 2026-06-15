"""Store invariant checks (CI gate).

Currently enforces the **bijection by existence** invariant (#1): every
``entries/<year>/<stem>.bib`` has exactly one ``meta/<year>/<stem>.json`` at the
same year + stem, and vice versa. The *content* of a sidecar may be empty
(``{}``), but the file must exist.

Run after the normalizer and before committing: a broken store fails the job
(non-zero exit + ``::error::`` annotations) so it is never committed. This is the
deliberate "fail loudly" backstop — we do **not** try to auto-heal a contributor
who moved a ``.bib`` without its sidecar; we surface it for a human to fix.
"""

from __future__ import annotations

import argparse
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Check store invariants (bijection by existence).")
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

    if bib_without_meta or meta_without_bib:
        print(
            f"FAIL: bijection broken — {len(bib_without_meta)} entry orphan(s), "
            f"{len(meta_without_bib)} sidecar orphan(s)"
        )
        sys.exit(1)
    print("OK: bijection holds (every .bib has its .json and vice versa)")


if __name__ == "__main__":
    main()
