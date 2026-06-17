"""Full-store gate: verify the store invariants S1–S5 across every entry.

The authoritative, deterministic, secret-free, pure-Python check (``pubstore-check``)
— the read-only counterpart of the diff job. Unlike the diff job it does **not**
assume the store is already valid: it parses every ``.bib``, re-derives canonical
form, and checks all of S1–S5 across the whole store. A merge is blocked (exit 1,
``::error::`` annotations) unless it passes. The store is small (thousands of
entries), so it just runs the whole thing.

Invariants (full statements in the publication store plan; old long-form ``#1–#12``
→ concise S1–S5 in parentheses):

- **S1 — Placement** (old #2 year-shard + #12 stem==citekey). Each entry lives at
  ``entries/<year>/<citekey>.bib`` with ``<year>`` its own ``year`` field and the
  stem equal to its citekey. Checked per entry in
  :func:`publication_store.entry.check_entry`.
- **S2 — Uniqueness** (old #10, the filename half). Citekeys are globally unique.
  An **O(N) filename check**: stems equal citekeys (S1), so a stem appearing in more
  than one shard is a duplicate — no quadratic compare, no parse for this check.
- **S3 — Pairing** (old #1 bijection). Every ``.bib`` has its ``.json`` at the same
  stem and vice versa. The forward half (``.bib`` → sidecar) is in ``check_entry``;
  the reverse half (orphan sidecars) is checked here.
- **S4 — Well-formed** (old #3 canonical bib + #4 sidecar shape). Each ``.bib`` is at
  the formatter fixpoint and holds one entry; each ``.json`` validates the schema.
  Per entry in ``check_entry``; orphan sidecars are shape-checked here too.
- **S5 — Closure** (new; the old plan had no explicit closure gate). The only files
  under ``entries/``/``meta/`` are the canonical ``<year>/<stem>`` pairs — no stray
  ``.bib``/``.json``, no leftover monolith.

Compiling the joined ``all.bib`` / ``<group>.bib`` / ``meta.json`` views is a
**separate add-on** (``pubstore-compile`` in :mod:`publication_store.artifacts`),
never part of this gate: a ``pubstore-check`` run does S1–S5 only. Semantic
deduplication is likewise deliberately **not** a gate — it is the out-of-band,
human-reviewed sweep (see the plan).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import entry, sidecar


def bib_paths(repo_root: Path) -> list[Path]:
    return sorted((Path(repo_root) / "entries").rglob("*.bib"))


def entry_errors(repo_root: Path) -> list[str]:
    """S1, S3 (forward), S4 — map :func:`entry.check_entry` over every ``.bib``."""
    repo_root = Path(repo_root)
    errors: list[str] = []
    for bib_path in bib_paths(repo_root):
        shard, stem = bib_path.parent.name, bib_path.stem
        meta_path = repo_root / "meta" / shard / f"{stem}.json"
        errors.extend(entry.check_entry(bib_path, meta_path, repo_root))
    return errors


def orphan_sidecar_errors(repo_root: Path) -> list[str]:
    """S3 (reverse) + S4 — sidecars with no matching ``.bib`` (and any that are malformed).

    The half :func:`entry.check_entry` (keyed on ``.bib`` files) cannot see. Each
    orphan is reported; an orphan that also fails the schema is flagged too, so the
    "every sidecar validates" guarantee holds store-wide. *Paired* sidecars are not
    re-validated here — ``check_entry`` already did that, so a malformed paired
    sidecar is reported exactly once.
    """
    repo_root = Path(repo_root)
    meta_dir = repo_root / "meta"
    errors: list[str] = []
    for meta_path in sorted(meta_dir.rglob("*.json")):
        rel = meta_path.relative_to(repo_root)
        shard_rel = meta_path.relative_to(meta_dir).with_suffix(".bib")
        if (repo_root / "entries" / shard_rel).exists():
            continue  # paired — check_entry handles its shape/JSON
        errors.append(
            f"{rel} has no entry entries/{shard_rel} — orphan sidecar "
            f"(did a .bib move without its sidecar? move the .json to match, or revert "
            f"the move and edit the entry in place so the bot relocates both)"
        )
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{rel} is not valid JSON ({exc})")
            continue
        message = sidecar.validation_error(data, str(rel), repo_root)
        if message is not None:
            errors.append(message)
    return errors


def citekey_collision_errors(repo_root: Path) -> list[str]:
    """S2 — O(N) filename check: a stem (== citekey by S1) appearing in >1 shard.

    Pure filenames, no parse: ``entries/<year>/<stem>.bib`` keyed by ``<stem>``. The
    per-file ``stem == citekey`` assertion (S1) is what lets this stand in for true
    citekey uniqueness without a quadratic compare.
    """
    repo_root = Path(repo_root)
    by_stem: dict[str, list[Path]] = {}
    for path in bib_paths(repo_root):
        by_stem.setdefault(path.stem, []).append(path.relative_to(repo_root))
    errors: list[str] = []
    for stem, paths in sorted(by_stem.items()):
        if len(paths) > 1:
            locations = ", ".join(str(p) for p in sorted(paths))
            errors.append(
                f"citekey {stem!r} appears in {len(paths)} shards ({locations}) "
                f"— global citekey uniqueness broken (S2)"
            )
    return errors


def closure_errors(repo_root: Path) -> list[str]:
    """S5 — the only files under ``entries/``/``meta/`` are the canonical pairs.

    Every file under ``entries/`` must be ``entries/<year>/<stem>.bib`` (a ``.bib``
    nested exactly one directory deep); every file under ``meta/`` must be
    ``meta/<year>/<stem>.json``. Anything else — a stray ``.bib`` at the ``entries``
    root, a loose ``.json``, a wrongly-nested file, a top-level drop, or a leftover
    monolith at the repo root — is a closure violation.
    """
    repo_root = Path(repo_root)
    errors: list[str] = []

    for base, suffix in (("entries", ".bib"), ("meta", ".json")):
        base_dir = repo_root / base
        if not base_dir.is_dir():
            continue
        for path in sorted(base_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(repo_root)
            parts = path.relative_to(base_dir).parts
            if path.suffix != suffix or len(parts) != 2:
                errors.append(
                    f"{rel}: unexpected file under {base}/ "
                    f"(S5 closure: only {base}/<year>/<stem>{suffix})"
                )

    # Loose drops / leftover monolith at the repo root (raw .bib, sibling .json map,
    # or the pre-store tueai_publications.bib/.json single-file layout).
    for path in sorted(repo_root.glob("*.bib")) + sorted(repo_root.glob("*.json")):
        errors.append(
            f"{path.relative_to(repo_root)}: stray file at the repo root "
            f"(S5 closure: entries live under entries/, sidecars under meta/)"
        )

    return errors


def check_store(repo_root: Path) -> list[str]:
    """Run all of S1–S5 over the store; return the flat list of error messages."""
    return [
        *entry_errors(repo_root),
        *orphan_sidecar_errors(repo_root),
        *citekey_collision_errors(repo_root),
        *closure_errors(repo_root),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Check the publication store invariants S1–S5 (the authoritative gate)."
    )
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    args = ap.parse_args()

    errors = check_store(args.root)
    for message in errors:
        print(f"::error::{message}")
    if errors:
        print(f"FAIL: {len(errors)} store invariant violation(s) (S1–S5)")
        sys.exit(1)

    print("OK: store satisfies S1–S5 (placement, uniqueness, pairing, well-formed, closure)")


if __name__ == "__main__":
    main()
