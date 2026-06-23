"""Add-on — group directories (``pubstore-groups``): drop association + browse mirror.

A **separable** add-on that sits *beside* the core (the two existing CLIs and the
S1–S5 logic are not touched). Two deliberately independent parts that share only
the ``groups/`` namespace, both treating the sidecar's ``custom.groups`` set as the
**single source of truth** for membership:

- **Part A — implicit association on drop** (``associate``): a ``groups/<slug>/``
  drop becomes a stored entry **plus** the slug unioned into ``custom.groups``;
  ``groups/`` is a transient inbox (the drop is consumed). The per-MR diff job.
- **Part B — symlink browse mirror** (``rebuild_mirror`` /
  ``mirror_consistency_errors``): the ``groups/`` tree as relative symlinks into the
  real entries, a *derived view* rebuilt from ``custom.groups``, owned by its own
  push-to-main workflow. Independent of A — it reads ``custom.groups`` however it was
  populated. See the Part B section below.

Part A turns a contributor's ``groups/<slug>/<file>.bib`` drop into a normal stored
entry **plus** a group-membership fact: the slug is **unioned** into that entry's
sidecar ``custom.groups``. No form, no JSON, no need to know the citekey scheme
beyond the directory name.

``custom.groups`` (pinned in ``schema/sidecar.schema.json``) is the **single source
of truth** for membership; the ``groups/`` directory is a *transient inbox* — a drop
is consumed (the entry lands canonically under ``entries/<year>/<key>.bib`` and the
drop is deleted), so this add-on leaves nothing behind in ``groups/`` (which lets the
follow-up symlink-mirror add-on own the whole tree).

Three outcomes of a drop, decided by the drop's content *after canonicalization* vs
the stored entry (see :func:`associate`):

- **New key** → a brand-new entry is created and added to the group.
- **Existing key, content matches** → "add this entry to the group": union the slug,
  leave the entry untouched, delete the drop (the move-into-group gesture).
- **Existing key, content differs** → **fail loudly** (the same-shard overwrite guard);
  a group drop must never be a backdoor edit of a stored entry.

Design notes:

- **Reuse, don't reimplement.** Placement is done by calling
  :func:`publication_store.diff_job.normalize_changed` as a library, so the core
  S1/S3/S4/S5 logic stays the single implementation. This module only adds the
  ``custom.groups`` union on top.
- **Collapse before the core.** Two drops of the *same* paper into two folders is a
  legitimate "assign both groups," yet they derive **one** path — which the core's
  within-MR batch guard (``pr9``) would reject. So :func:`associate` collapses by
  derived path *first* (union the slug-sets if the canonical text matches, fail if it
  differs), then hands the core one representative per path.
- **Union, never overwrite.** A drop only ever *adds* a slug; one human-added group
  never drops the scraper's others. Removing a group is a deliberate sidecar edit.
- **Slugs are verbatim** the directory name — no lowercasing/normalization, no schema
  enum, no PI-table validation (shape-not-vocabulary, like the citekey scheme). The
  committed ``groups/<slug>/`` directories are the discoverable menu + soft typo-guard.

Deps stay ``zotero-rdf`` + ``jsonschema`` (no ``publib``); group membership is
*report data*, never a gated invariant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import entry, sidecar
from .diff_job import NormalizeError, normalize_changed
from .store import bib_paths, meta_path_for


def slug_of(path: Path, root: Path) -> str | None:
    """The group slug for a ``groups/<slug>/<file>.bib`` drop, or ``None`` if not one.

    The directory name **verbatim** (no lowercasing, no charset normalization — the
    same hands-off stance as the citekey scheme). Returns ``None`` for anything that
    is not a regular ``.bib`` file sitting exactly one directory deep under
    ``groups/`` — a wrong depth, a non-``.bib`` suffix, a path outside ``groups/``,
    or a **symlink** (the derived browse-mirror, owned by a separate add-on). A
    ``None`` on a real (non-symlink) path under ``groups/`` is a malformed group path
    the caller surfaces loudly.
    """
    path = Path(path)
    if path.is_symlink():
        return None  # derived browse-mirror entry — never an inbox drop
    root = Path(root)
    try:
        rel = path.resolve().relative_to((root / "groups").resolve())
    except ValueError:
        return None  # not under groups/
    parts = rel.parts
    if len(parts) != 2 or path.suffix != ".bib":
        return None  # expect exactly <slug>/<file>.bib
    return parts[0]


def union_group(meta_path: Path, slug: str, root: Path) -> bool:
    """Union ``slug`` into a sidecar's ``custom.groups`` (dedupe + sort). Returns changed?

    Loads via :func:`publication_store.sidecar.split_sidecar`, so a flat/legacy
    sidecar is **rejected, not silently overwritten** (S4 consistency). Rewrites the
    sidecar with the *same* ``json.dumps`` shape the diff job uses, so the result is
    byte-identical to what a normalize pass would produce (no churn, idempotent: a
    second union of an already-present slug rewrites the same bytes). All other
    ``custom`` keys and the whole ``zotero`` half survive verbatim.
    """
    meta_path = Path(meta_path)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    zotero, custom = sidecar.split_sidecar(data, str(meta_path), root)
    groups = sorted(set(custom.get("groups", [])) | {slug})
    new_custom = {**custom, "groups": groups}
    changed = new_custom != custom
    meta_path.write_text(
        json.dumps({"zotero": zotero, "custom": new_custom}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return changed


def _collapse(root: Path, group_bibs: list[Path]):
    """Collapse the drops by derived path; filter symlinks; reject malformed paths.

    Returns ``(by_path, reps, leftovers)`` where ``by_path`` maps a relative derived
    ``.bib`` path to ``{"slugs": set, "text": str, "citekey": str, "year": str,
    "existed": bool}``, ``reps`` is the list of source files to hand to the core (one
    per derived path), and ``leftovers`` are the redundant duplicate drops (their
    paths were all already introduced by an earlier rep) that the core won't see and
    so ``associate`` must delete itself.

    Raises ``ValueError`` on a malformed group path or an unparseable drop, and
    :class:`NormalizeError` on a within-MR path clash with *differing* content (the
    ``pr9`` intent) or a partial-overlap that the core's batch guard couldn't collapse.
    """
    root = Path(root)
    by_path: dict[Path, dict] = {}
    rep_of: dict[Path, Path] = {}  # derived path -> the source file that introduced it
    src_paths: dict[Path, set[Path]] = {}  # source file -> all derived paths it produces
    clashes: list[str] = []

    for raw in group_bibs:
        raw = Path(raw)
        if raw.is_symlink():
            continue  # derived browse-mirror entry — not an inbox drop
        slug = slug_of(raw, root)
        if slug is None:
            raise ValueError(
                f"{raw}: not a valid group drop (expected groups/<slug>/<file>.bib)"
            )
        text = raw.read_text(encoding="utf-8")
        for citekey, year, entry_text, _ in entry.canonical_entries(text):
            bib_rel, _ = entry.derive_path(year, citekey)
            src_paths.setdefault(raw, set()).add(bib_rel)
            existing = by_path.get(bib_rel)
            if existing is None:
                by_path[bib_rel] = {
                    "slugs": {slug},
                    "text": entry_text,
                    "citekey": citekey,
                    "year": year,
                    "existed": (root / bib_rel).exists(),
                }
                rep_of[bib_rel] = raw
            elif existing["text"] != entry_text:
                clashes.append(
                    f"two group drops collide on one path {bib_rel} with different "
                    f"content (citekey {citekey!r} — S2 uniqueness; rename one key, or "
                    f"edit entries/{year}/{citekey}.bib directly if it is the same paper)"
                )
            else:
                existing["slugs"].add(slug)  # same paper into another folder — add its slug

    if clashes:
        raise NormalizeError("\n".join(clashes))

    reps = sorted(set(rep_of.values()), key=str)
    # A path must be produced by exactly one rep (collapse guarantee). The only way it
    # is not is a pathological partial overlap (a rep introduced by one of its paths
    # also re-produces a path owned by another rep) — fail loudly rather than let the
    # core's batch guard trip on a tree we built.
    produced: dict[Path, int] = {}
    for rep in reps:
        for bib_rel in src_paths[rep]:
            produced[bib_rel] = produced.get(bib_rel, 0) + 1
    overlap = sorted(str(p) for p, n in produced.items() if n > 1)
    if overlap:
        raise NormalizeError(
            "group drops overlap on path(s) "
            + ", ".join(overlap)
            + " — split them into separate changes or drop each paper once"
        )

    leftovers = [raw for raw in src_paths if raw not in set(reps)]
    return by_path, reps, leftovers


def associate(root: Path, changed_group_bibs: list[Path]):
    """Place the dropped ``groups/<slug>/`` entries and union their slugs.

    Orchestrates the add-on's flow (see the module docstring):

    1. **collapse** the drops by derived path (filter symlinks, reject malformed
       paths), unioning the slug-sets of same-paper drops so a one-MR multi-group
       assignment never trips the core's batch guard;
    2. call :func:`normalize_changed` **once** with one representative per path — the
       untouched core does the canonical placement, sidecar creation, and raw-drop
       deletion;
    3. **union** every slug for each produced entry into its sidecar ``custom.groups``;
    4. delete the redundant duplicate drops the core never saw.

    Returns ``(written, removed, messages)``. ``messages`` are human-facing lines
    distinguishing "created <citekey> in group(s) …" (new key) from "added <citekey>
    to group(s) …" (existing key, identical content — the move-into-group gesture).
    Raises :class:`NormalizeError` on a within-MR clash or — surfaced from the core
    and **reframed** — a drop that would change a *stored* entry (edit the entry
    directly; drop it unchanged to only add a group). Raises ``ValueError`` on a
    malformed group path or an unparseable drop.
    """
    root = Path(root)
    by_path, reps, leftovers = _collapse(root, changed_group_bibs)

    if not by_path:
        return [], [], []

    try:
        written, removed, _ = normalize_changed(root, reps)
    except NormalizeError as exc:
        # The core rejected a drop that would overwrite a *different* stored entry
        # (existing key, differing content) or a cross-shard duplicate. Reframe so the
        # contributor sees the two intents rather than the generic duplicate text.
        raise NormalizeError(
            f"{exc}\n"
            f"a group drop must not change a stored entry: drop it UNCHANGED to only "
            f"add the group, or edit entries/<year>/<key>.bib directly to change the entry"
        ) from exc

    messages: list[str] = []
    for bib_rel in sorted(by_path, key=str):
        info = by_path[bib_rel]
        _, meta_rel = entry.derive_path(info["year"], info["citekey"])
        for slug in sorted(info["slugs"]):
            union_group(root / meta_rel, slug, root)
        verb = "added" if info["existed"] else "created"
        prep = "to group(s)" if info["existed"] else "in group(s)"
        messages.append(f"{verb} {info['citekey']} {prep} {', '.join(sorted(info['slugs']))}")

    # The core only unlinked the representative drops; the duplicate drops it never saw
    # (same paper into another folder, already consumed via its rep) are ours to clean.
    for raw in leftovers:
        raw = Path(raw)
        if raw.exists():
            raw.unlink()
            removed.append(raw)

    return written, removed, messages


# --------------------------------------------------------------------------- #
# Part B — symlink browse mirror (derived view, owned by its own workflow)
#
# `groups/<slug>/<citekey>.bib` as a *relative symlink* into the real
# `entries/<year>/<citekey>.bib`, so the repo UI shows each group's papers as a
# folder. Strictly **derived** from `custom.groups` (the single source of truth)
# and rebuilt wholesale on every run — a manual edit to the tree is reverted on
# the next rebuild. The mirror is **never** part of the S1–S5 gate: the closure
# check (S5) is blind to `groups/`, so the tree is a view like the compiled
# artifacts. Its own consistency is surfaced by `mirror_consistency_errors`
# (via the `--check` flag), never folded into `pubstore-check`.
#
# Independent of Part A: these read `custom.groups` however it was populated (a
# Part A drop *or* a scraper/agent sidecar edit) and own the whole `groups/`
# tree as symlinks — built only once Part A is in use and browse-by-group is
# wanted. The committed `.gitkeep` skeleton (one per known PI slug) is never
# touched here; only symlinks are wiped and recreated.
# --------------------------------------------------------------------------- #

_GROUPS = "groups"


def _entry_memberships(root: Path) -> list[tuple[str, str, list[str]]]:
    """``(citekey, year, sorted-unique groups)`` for every entry with a non-empty ``custom.groups``.

    Keyed off the merged store state, not a diff: walks ``entries/`` and reads
    each entry's sidecar ``custom.groups``. Defensive (the mirror runs post-merge
    on an already-green store, and must never fail the workflow): a missing or
    unparseable sidecar is skipped rather than raised on.
    """
    root = Path(root)
    out: list[tuple[str, str, list[str]]] = []
    for bib_path in bib_paths(root):
        year, citekey = bib_path.parent.name, bib_path.stem
        meta_path = meta_path_for(bib_path, root)
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        groups = data.get("custom", {}).get("groups", []) if isinstance(data, dict) else []
        if groups:
            out.append((citekey, year, sorted(set(groups))))
    return out


def _mirror_symlinks(root: Path) -> list[Path]:
    """Every symlink currently under ``groups/`` — the mirror, *never* the ``.gitkeep`` skeleton.

    Only symlinks are the mirror; regular files (a transient Part A drop, the
    committed ``.gitkeep`` placeholders, ``README.md``) are left untouched.
    """
    groups_dir = Path(root) / _GROUPS
    if not groups_dir.is_dir():
        return []
    return sorted(p for p in groups_dir.rglob("*") if p.is_symlink())


def rebuild_mirror(root: Path) -> tuple[list[Path], list[Path]]:
    """Rebuild the ``groups/`` symlink mirror from ``custom.groups``. Returns ``(created, removed)``.

    Wipe the existing mirror **symlinks only** (the ``.gitkeep`` skeleton and any
    other regular file are left in place), then for every entry, for every slug in
    its ``custom.groups``, recreate ``groups/<slug>/<citekey>.bib`` as a
    **relative** symlink to ``entries/<year>/<citekey>.bib`` (relative so it
    survives clone/checkout — every ``groups/<slug>/`` sits at the same depth, so
    the link is always ``../../entries/<year>/<citekey>.bib``). Idempotent: a clean
    rebuild each run, so re-running on an unchanged store reproduces byte-identical
    links and git sees no change.
    """
    root = Path(root)
    removed: list[Path] = []
    for link in _mirror_symlinks(root):
        link.unlink()
        removed.append(link)

    created: list[Path] = []
    for citekey, year, groups in _entry_memberships(root):
        bib_abs = root / "entries" / year / f"{citekey}.bib"
        for slug in groups:
            link = root / _GROUPS / slug / f"{citekey}.bib"
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()  # a stale same-named regular drop, or a just-made link
            link.symlink_to(os.path.relpath(bib_abs, start=link.parent))
            created.append(link)
    return created, removed


def mirror_consistency_errors(root: Path) -> list[str]:
    """Mirror drift vs ``custom.groups`` (the source of truth). Empty list == consistent.

    Surfaced via ``pubstore-groups rebuild-mirror --check`` — **never** folded into
    the S1–S5 gate (``pubstore-check``). Flags, with repo-root-relative messages:

    - a mirror symlink at the wrong depth / suffix (not ``groups/<slug>/<key>.bib``);
    - a **broken** symlink (target does not resolve — the hand-broken case);
    - a symlink pointing somewhere other than ``entries/<year>/<key>.bib``;
    - a **stale** link whose entry's ``custom.groups`` no longer lists ``<slug>``;
    - a **missing** link for an ``(entry, slug)`` membership that should be mirrored.
    """
    root = Path(root)
    errors: list[str] = []

    expected: set[tuple[str, str]] = set()  # (slug, citekey)
    year_of: dict[str, str] = {}
    for citekey, year, groups in _entry_memberships(root):
        year_of[citekey] = year
        for slug in groups:
            expected.add((slug, citekey))

    actual: set[tuple[str, str]] = set()
    for link in _mirror_symlinks(root):
        rrel = link.relative_to(root)
        rel = link.relative_to(root / _GROUPS)
        if len(rel.parts) != 2 or link.suffix != ".bib":
            errors.append(f"{rrel}: stray mirror symlink (expected groups/<slug>/<citekey>.bib)")
            continue
        slug, citekey = rel.parts[0], link.stem
        try:
            resolved = link.resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append(f"{rrel}: broken symlink (target does not resolve)")
            continue
        want_year = year_of.get(citekey)
        want = (root / "entries" / want_year / f"{citekey}.bib").resolve() if want_year else None
        if want is None or resolved != want:
            errors.append(
                f"{rrel}: symlink does not point at entries/<year>/{citekey}.bib"
            )
            continue
        if (slug, citekey) not in expected:
            errors.append(
                f"{rrel}: entry {citekey} is not in group '{slug}' (custom.groups) — stale mirror link"
            )
            continue
        actual.add((slug, citekey))

    for slug, citekey in sorted(expected - actual):
        errors.append(
            f"groups/{slug}/{citekey}.bib: missing mirror symlink for entry {citekey} in group '{slug}'"
        )
    return errors


def _associate_main(args) -> None:
    try:
        written, removed, messages = associate(args.root, args.paths)
    except NormalizeError as exc:
        for line in str(exc).splitlines():
            print(f"::error::{line}")
        sys.exit(1)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"::error::group association aborted (malformed input, nothing written): {exc}")
        sys.exit(1)
    print(
        f"associated {len(messages)} entr{'y' if len(messages) == 1 else 'ies'}; "
        f"wrote {len(written)} files; removed {len(removed)} drop(s)"
    )
    for m in messages:
        print(f"  {m}")


def _rebuild_mirror_main(args) -> None:
    if args.check:
        errors = mirror_consistency_errors(args.root)
        for message in errors:
            print(f"::error::{message}")
        if errors:
            print(f"FAIL: {len(errors)} groups/ mirror inconsistency(ies)")
            sys.exit(1)
        print("OK: groups/ mirror is consistent with custom.groups")
        return
    created, removed = rebuild_mirror(args.root)
    print(
        f"rebuilt groups/ mirror: {len(created)} symlink(s) from custom.groups, "
        f"{len(removed)} pre-existing symlink(s) cleared"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="pubstore-groups",
        description="Group-directory add-on: associate a groups/<slug>/ drop with its "
        "entry by unioning the slug into custom.groups (the source of truth).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_assoc = sub.add_parser(
        "associate",
        help="place groups/<slug>/*.bib drops and union the slug into custom.groups",
    )
    p_assoc.add_argument("paths", nargs="+", type=Path, help="changed groups/<slug>/*.bib drops")
    p_assoc.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    p_assoc.set_defaults(func=_associate_main)

    p_mirror = sub.add_parser(
        "rebuild-mirror",
        help="rebuild the groups/<slug>/ symlink browse mirror from custom.groups "
        "(the source of truth); a derived view, owned by its own push-to-main workflow",
    )
    p_mirror.add_argument(
        "--check",
        action="store_true",
        help="read-only: report mirror drift vs custom.groups and exit 1 on any "
        "(never rebuilds, never part of pubstore-check)",
    )
    p_mirror.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    p_mirror.set_defaults(func=_rebuild_mirror_main)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
