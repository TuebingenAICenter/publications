"""Per-MR diff job: normalize the changed entries into the store layout.

The default contribution path (``pubstore-normalize``). Given the set of ``.bib``
files changed in a merge request (located anywhere in the repo — a raw paste, a
loosely-placed drop, or an in-place edit of a stored entry), it rewrites each
into the canonical store layout

    entries/<year>/<citekey>.bib    one canonical BibTeX entry per file
    meta/<year>/<citekey>.json      matching sidecar, {"zotero": ..., "custom": ...}

and removes the raw source files. It **assumes the store already satisfies
S1–S5** and only has to fix and verify the *changed* entries plus confirm they
don't break the one global invariant (S2). The same path serves the initial
big-data MR and every future one-entry MR — there is no separate migration tool.

Store invariants it establishes / checks (full statements in the publication
store plan):

- **S1 — Placement.** Each entry is written to its *derived* path
  ``entries/<year>/<citekey>.bib`` (+ matching ``meta/``), where ``<year>`` is the
  entry's own ``year`` (or ``undated``) and the stem **is the citekey** — never the
  contributor's filename. A drop named ``my_paper.bib`` holding
  ``@article{smith_2025,…}`` lands at ``entries/2025/smith_2025.bib`` and the
  original is deleted. See :func:`publication_store.entry.derive_path`.
- **S2 — Uniqueness (diff-scoped).** For each new/changed citekey, a cheap
  filename lookup across the year shards (no parse of the store) fails loudly if a
  *different* path already holds that key — the cross-shard duplicate the
  filesystem can't catch on its own (the ``pr6`` sequential case). A stray drop that
  would land on an existing *same-path* entry with different content (same citekey +
  year, from a filename git's add/add conflict didn't catch — the ``pr8`` case) fails
  loudly too rather than overwrite it, as do two entries in one change that collide on
  one derived path (the ``pr9`` case). The *concurrent* cross-shard case is
  deliberately ungated (a transient duplicate — see the plan).
- **S3 — Pairing.** Every written ``.bib`` gets its ``.json`` sidecar at the same
  stem (created as ``{"zotero": …, "custom": …}``, possibly both ``{}``).
- **S4 — Well-formed.** Each entry is re-emitted via ``to_bibtex`` (the formatter
  fixpoint); the sidecar is validated, never migrated. An **idempotency
  self-check** re-runs canonicalization on the just-written output and fails rather
  than commit a tree the tool can't reproduce.
- **S5 — Closure.** Raw drops, sibling ``.json`` maps, and relocation leftovers are
  deleted so only the canonical pairs remain.

Other mechanics:

- **relocation carries data** — an edit that changes an entry's year moves the pair
  into the new year directory and transfers the existing ``custom`` report data,
  deleting the old pair (no orphan, no data loss).
- **custom-half preservation** — re-normalizing keeps any existing ``custom`` data
  and only refreshes the ``zotero`` half.
- **multi-entry paste** — a paste of N entries is split **per parsed item**
  (``from_bibtex`` → ``to_bibtex`` → per-entry), not by splitting text on blank
  lines, then each item is placed as above (the ``pr2`` case).
- **collision seam (hard)** — writing onto a *different* pre-existing entry at the
  same path (a same-shard overwrite git did not catch — the ``pr8`` case) fails
  loudly before any write, never a silent overwrite; a human renames the key or
  reconciles. An *identical* re-drop is a no-op (nothing to resolve).

This tool is deterministic, secret-free, and pure-Python; "find changed ``.bib``"
is the workflow's job (it takes an explicit file list), and pushing the result
back to the MR branch is the workflow's job too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import entry
from .sidecar import split_sidecar


class NormalizeError(Exception):
    """A hard failure that must block the MR: an S2 collision or a non-idempotent result.

    Distinct from a ``ValueError`` (malformed input): these are *valid* inputs whose
    result the store refuses to accept.
    """


def _load_store_sidecar(meta_path: Path, root: Path) -> tuple[dict, dict]:
    """Read a stored sidecar's ``(zotero, custom)`` halves, failing loudly on bad shape.

    Validates against ``schema/sidecar.schema.json``: a bare ``{}`` or a missing half
    defaults to ``{}``, but a flat / legacy sidecar is **rejected, not migrated** —
    silently reading it as ``.get("zotero", {})`` would drop the whole overlay (the
    ``pr7`` data-loss path; S4). The fix is a human one (wrap it, or empty it).
    """
    if not meta_path.exists():
        return {}, {}
    data = json.loads(meta_path.read_text(encoding="utf-8"))  # JSONDecodeError → fail
    return split_sidecar(data, str(meta_path), root)


def _in_store_meta(repo_root: Path, bib_path: Path) -> Path | None:
    """The ``meta/`` path for a ``.bib`` that already lives under ``entries/``."""
    try:
        rel = bib_path.resolve().relative_to((repo_root / "entries").resolve())
    except ValueError:
        return None
    return repo_root / "meta" / rel.with_suffix(".json")


def _existing_key_paths(repo_root: Path, citekey: str) -> list[Path]:
    """S2 — every ``entries/<year>/<citekey>.bib`` on disk for this key (filename lookup).

    A directory listing keyed on the filename (stems equal citekeys by S1), one stat
    per year shard — **never a parse of the store**. Avoids ``glob`` so a citekey
    containing a glob metacharacter still matches exactly.
    """
    entries_dir = repo_root / "entries"
    if not entries_dir.is_dir():
        return []
    found: list[Path] = []
    for shard in entries_dir.iterdir():
        if shard.is_dir():
            candidate = shard / f"{citekey}.bib"
            if candidate.exists():
                found.append(candidate)
    return found


def _plan_file(repo_root: Path, bib_path: Path):
    """Plan the targets for one changed ``.bib``.

    Returns ``(targets, extra_sources, carried_custom)`` where each target is
    ``(citekey, year, entry_text, zotero)``. ``carried_custom`` is the existing
    ``custom`` half to carry when a single in-store entry relocates; ``extra_sources``
    are sibling raw files (a drop's ``<stem>.json``) to delete.
    """
    text = bib_path.read_text(encoding="utf-8")
    in_store_meta = _in_store_meta(repo_root, bib_path)

    # Sidecar for from_bibtex: in-store entries take the zotero half from their stored
    # meta (keyed by the entry's *internal* citekey, which may differ from the
    # filename); raw drops take a sibling {citekey: sidecar} map.
    extra_sources: list[Path] = []
    if in_store_meta is not None:
        zotero, carried_custom = _load_store_sidecar(in_store_meta, repo_root)
        ck = entry.citekey_of(text) if in_store_meta.exists() else None
        sidecar_map = {ck: zotero} if ck else None
    else:
        sibling = bib_path.with_suffix(".json")
        sidecar_map = json.loads(sibling.read_text(encoding="utf-8")) if sibling.exists() else None
        if sibling.exists():
            extra_sources.append(sibling)
        carried_custom = {}

    targets = entry.canonical_entries(text, sidecar_map)
    single = len(targets) == 1
    return targets, extra_sources, (carried_custom if single else {})


def _collision_errors(repo_root: Path, plans, changed_set: set[Path]) -> list[str]:
    """S2 — diff-scoped cross-shard collisions for the planned targets (pre-write).

    A planned target collides if some *other* path already holds its citekey and that
    path is not one of the MR's own changed sources (which are about to be moved or
    rewritten — e.g. the ``2025/`` source of a ``2025→2026`` relocation, the ``pr4``
    case, is not a collision). A match at the *same* path is an in-place
    re-normalization, also not a collision.
    """
    errors: list[str] = []
    for _, targets, _, _ in plans:
        for citekey, year, _, _ in targets:
            bib_rel, _ = entry.derive_path(year, citekey)
            target = (repo_root / bib_rel).resolve()
            for existing in _existing_key_paths(repo_root, citekey):
                resolved = existing.resolve()
                if resolved == target or resolved in changed_set:
                    continue
                errors.append(
                    f"citekey {citekey!r} already exists at {existing.relative_to(repo_root)} "
                    f"but this change would add it at {bib_rel} (cross-shard duplicate — S2 "
                    f"uniqueness; rename the key or reconcile with the existing entry)"
                )
    return errors


def _overwrite_errors(repo_root: Path, plans, changed_set: set[Path]) -> list[str]:
    """S2 — a planned target would overwrite a *different* entry already at its path.

    Same citekey **and** same year as a stored entry, but arriving from a *different*
    source (a stray drop, not the entry's own in-place edit), so the two derive the
    identical path. A literal same-path add is caught earlier by git's add/add
    conflict, but a drop placed elsewhere (``incoming.bib``) slips past git — and
    silently clobbering the stored entry would lose a publication. So we **fail
    loudly before any write** (the same disposition as the cross-shard ``pr6`` case,
    just caught at the same path) and let a human rename the key or reconcile.

    Two writes onto an existing path are *not* collisions: the entry's own
    re-normalization / in-place edit (source path == target, in ``changed_set``), and
    an *identical* re-drop (byte-for-byte the stored canonical text — nothing to
    resolve, a harmless no-op).
    """
    errors: list[str] = []
    for _, targets, _, _ in plans:
        for citekey, year, entry_text, _ in targets:
            bib_rel, _ = entry.derive_path(year, citekey)
            bib_path = repo_root / bib_rel
            if not bib_path.exists():
                continue
            if bib_path.resolve() in changed_set:
                continue  # the entry's own re-normalization / in-place edit (pr3)
            if bib_path.read_text(encoding="utf-8") == entry_text:
                continue  # identical re-drop — a harmless no-op, nothing to resolve
            errors.append(
                f"citekey {citekey!r} already exists at {bib_rel} with different "
                f"content; this change would overwrite it (same-shard duplicate — S2 "
                f"uniqueness; rename the key or reconcile with the existing entry)"
            )
    return errors


def _batch_collision_errors(plans) -> list[str]:
    """S2 — two entries in the *same* change that derive the *same* path.

    The within-MR analogue of :func:`_overwrite_errors`: two distinct source files
    whose canonical (year, citekey) land on one derived path. Neither is on disk yet,
    so the on-disk checks can't see it — and left unchecked the second write silently
    clobbers the first while *both* raw sources are deleted, a lost publication with no
    signal (git doesn't catch it either: two source filenames, no add/add conflict). A
    single file's duplicate keys are already rejected upstream by the BibTeX parser;
    this catches the cross-file case. Fail loudly so a human renames a key.
    """
    by_path: dict[Path, list[str]] = {}
    for _, targets, _, _ in plans:
        for citekey, year, _, _ in targets:
            bib_rel, _ = entry.derive_path(year, citekey)
            by_path.setdefault(bib_rel, []).append(citekey)
    errors: list[str] = []
    for bib_rel in sorted(by_path, key=str):
        keys = by_path[bib_rel]
        if len(keys) > 1:
            errors.append(
                f"this change produces {len(keys)} entries that collide on one path "
                f"{bib_rel} (citekey {keys[0]!r} — S2 uniqueness; rename one key or "
                f"drop the duplicate)"
            )
    return errors


def _idempotency_errors(repo_root: Path, written_bibs: list[Path]) -> list[str]:
    """S4 — re-run canonicalization on the just-written output; it must be a fixpoint.

    The mutating counterpart of the checker's canonical-form check: if re-normalizing
    our own output would change its text or move it to a different derived path, the
    tool produced a tree it cannot reproduce, so we fail rather than commit it.
    """
    errors: list[str] = []
    for bib_path in written_bibs:
        rel = bib_path.relative_to(repo_root)
        text = bib_path.read_text(encoding="utf-8")
        try:
            citekey, year, canonical, _ = entry.canonical_entry(text)
        except ValueError as exc:
            errors.append(f"{rel}: {exc} (after normalize)")
            continue
        if canonical != text:
            errors.append(
                f"{rel} is not at the normalizer fixpoint (re-normalizing would rewrite it)"
            )
        bib_rel, _ = entry.derive_path(year, citekey)
        if (repo_root / bib_rel).resolve() != bib_path.resolve():
            errors.append(
                f"{rel} would relocate to {bib_rel} on re-normalize (non-idempotent placement)"
            )
    return errors


def normalize_changed(repo_root: Path, changed_bibs: list[Path]):
    """Normalize the ``.bib`` files changed in an MR; relocate + delete raw sources.

    Returns ``(written, removed, warnings)``. Raises :class:`NormalizeError` on an S2
    cross-shard collision (before writing anything) or a non-idempotent result (after
    writing — the workflow then does not commit). Raises ``ValueError`` /
    ``json.JSONDecodeError`` on malformed input.
    """
    repo_root = Path(repo_root)
    changed_set = {Path(b).resolve() for b in changed_bibs}

    plans = []  # (source_bib, targets, extra_sources, carried_custom)
    for bib in changed_bibs:
        bib = Path(bib)
        if not bib.exists():  # deleted in the MR — nothing to parse
            continue
        targets, extra, carried = _plan_file(repo_root, bib)
        plans.append((bib, targets, extra, carried))

    # S2 (plan step 4): block a duplicate before writing anything — a cross-shard
    # re-add (different path, same key), a same-path overwrite of a different entry,
    # or two entries in this same change that collide on one derived path.
    collisions = _collision_errors(repo_root, plans, changed_set)
    overwrites = _overwrite_errors(repo_root, plans, changed_set)
    batch = _batch_collision_errors(plans)
    if collisions or overwrites or batch:
        raise NormalizeError("\n".join(collisions + overwrites + batch))

    target_bibs = {
        (repo_root / entry.derive_path(year, citekey)[0]).resolve()
        for _, targets, _, _ in plans
        for (citekey, year, _, _) in targets
    }

    written: list[Path] = []
    warnings: list[str] = []
    for _, targets, _, carried in plans:
        for citekey, year, entry_text, zotero in targets:
            bib_rel, meta_rel = entry.derive_path(year, citekey)
            bib_path = repo_root / bib_rel
            meta_path = repo_root / meta_rel

            # A same-path overwrite of a *different* entry was already rejected
            # pre-write by ``_overwrite_errors``; reaching here means this write is a
            # legitimate (re-)normalization or a brand-new placement.
            _, existing_custom = _load_store_sidecar(meta_path, repo_root)
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

    # S4 (plan step 5): the normalizer must be a fixpoint on its own output.
    idempotency = _idempotency_errors(repo_root, [p for p in written if p.suffix == ".bib"])
    if idempotency:
        raise NormalizeError("\n".join(idempotency))

    return written, removed, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize changed .bib files into the store layout.")
    ap.add_argument("paths", nargs="+", type=Path, help="changed .bib files (from git diff)")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    args = ap.parse_args()
    try:
        written, removed, warnings = normalize_changed(args.root, args.paths)
    except NormalizeError as exc:
        for line in str(exc).splitlines():
            print(f"::error::{line}")
        sys.exit(1)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error::normalize aborted (malformed input, nothing written): {exc}")
        sys.exit(1)
    print(f"wrote {len(written)} files ({len(written) // 2} entries); removed {len(removed)} raw source(s)")
    for w in warnings:
        print(f"  ::warning:: {w}")


if __name__ == "__main__":
    main()
