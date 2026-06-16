"""Per-entry shared core for the publication store tooling.

Pure, single-entry functions shared by the two drivers — the diff job
(:mod:`publication_store.diff_job`) and the global checker
(:mod:`publication_store.checker`). No git, no argparse, no full-store walk: just
the S1/S4 logic applied to one entry (or one parsed paste), so both drivers agree
on canonical form and placement.

The store invariants these functions implement (full statements in the publication
store plan):

- **S1 — Placement.** Each entry sits at its *derived* path
  ``entries/<year>/<citekey>.bib`` + ``meta/<year>/<citekey>.json``, where
  ``<year>`` comes from the entry's ``year`` field (or ``undated``) and the stem
  is the citekey. The path is a pure function of (year, citekey): see
  :func:`derive_path`.
- **S4 — Well-formed.** Each ``.bib`` is at the formatter fixpoint
  (``to_bibtex(from_bibtex(x)) == x``) and holds exactly one entry; each ``.json``
  validates the sidecar schema. :func:`canonical_entry` /
  :func:`canonical_entries` re-emit canonical form; :func:`check_entry` verifies a
  pair on disk.

Invariant re-map (old long-form ``#1–#12`` → concise S1–S5): S1 = old #2
(year-shard) + #12 (stem==citekey); S3 = old #1 (bijection); S4 = old #3
(canonical bib) + #4 (sidecar shape). S2 (uniqueness) and S5 (closure) are
store-wide and live in :mod:`publication_store.checker` /
:mod:`publication_store.diff_job`, not here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from zotero_rdf import BibtexParseError, from_bibtex, to_bibtex

from . import sidecar

_CITEKEY_RE = re.compile(r"^@\w+\{([^,]+),", re.MULTILINE)
_YEAR_RE = re.compile(r"^\s*year\s*=\s*\{?(\d{4})", re.MULTILINE)
UNDATED = "undated"


def citekey_of(entry_text: str) -> str | None:
    """The citekey of a single ``.bib`` entry, or ``None`` if none is present."""
    m = _CITEKEY_RE.search(entry_text)
    return m.group(1) if m else None


def year_of(entry_text: str) -> str:
    """S1 — the year shard for an entry: its ``year`` field, or ``undated``."""
    m = _YEAR_RE.search(entry_text)
    return m.group(1) if m else UNDATED


def derive_path(item_year: str, citekey: str) -> tuple[Path, Path]:
    """S1 — the derived ``(bib, meta)`` relpaths for ``(year, citekey)``.

    A pure function of its inputs: ``entries/<year>/<citekey>.bib`` and
    ``meta/<year>/<citekey>.json`` (relative to the repo root). Path uniqueness
    follows from citekey uniqueness (S2), which is why the store can place an entry
    without an index — the key alone determines where it lives.
    """
    return (
        Path("entries") / item_year / f"{citekey}.bib",
        Path("meta") / item_year / f"{citekey}.json",
    )


def split_entries(bib_string: str) -> list[tuple[str, str]]:
    """Split ``to_bibtex`` output into ``(citekey, entry_text)`` pairs.

    Operates on the *formatter's own* output (blank-line separated, one entry per
    block), not on arbitrary contributor text — the per-item split that matters
    (one ``.bib`` per parsed ``ZoteroItem``) already happened in
    :func:`canonical_entries` via ``from_bibtex`` → ``to_bibtex``.
    """
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


def canonical_entries(
    text: str, sidecar_map: dict[str, dict] | None = None
) -> list[tuple[str, str, str, dict]]:
    """Parse a ``.bib`` (one or many entries) and re-emit each in canonical form.

    Returns one ``(citekey, year, entry_text, zotero_spill)`` per parsed
    ``ZoteroItem``. Splitting is **per parsed item** — re-emit one entry per item,
    never a blank-line split of contributor text — so a multi-entry paste (the
    ``pr2`` case) becomes one canonical entry per publication. ``sidecar_map`` is
    the ``{citekey: zotero_half}`` overlay fed back into ``from_bibtex`` so the
    round-trip stays lossless. Raises ``ValueError`` on a parse error or empty
    input.
    """
    try:
        items = from_bibtex(text, sidecar=sidecar_map)
    except BibtexParseError as exc:
        raise ValueError(f"does not parse as BibTeX ({exc})") from exc
    if not items:
        raise ValueError("contains no BibTeX entries")
    bib_string, spill = to_bibtex(items)
    return [
        (citekey, year_of(entry_text), entry_text, spill.get(citekey, {}))
        for citekey, entry_text in split_entries(bib_string)
    ]


def canonical_entry(text: str) -> tuple[str, str, str, dict]:
    """S4 — canonicalize a *single* ``.bib`` entry; assert one entry per file.

    Returns ``(citekey, year, entry_text, zotero_spill)``. Raises ``ValueError`` on
    a parse error or if the file does not hold exactly one entry. This is the
    fixpoint oracle behind both the checker's canonical-form check and the diff
    job's idempotency self-check.
    """
    entries = canonical_entries(text)
    if len(entries) != 1:
        raise ValueError(
            f"holds {len(entries)} entries — the store is one entry per file"
        )
    return entries[0]


def check_entry(bib_path: Path, meta_path: Path, root: Path) -> list[str]:
    """Read-only per-pair predicates (S1, S3 pairing, S4). Empty list == OK.

    Given a ``.bib`` under ``entries/`` and its matching ``meta/`` sidecar path,
    check:

    - **S4** the ``.bib`` parses to exactly one entry and is at the formatter
      fixpoint (the normalizer would not rewrite it);
    - **S1** the filename stem equals the citekey, and the year shard equals the
      entry's ``year`` field;
    - **S3** the sidecar exists (the forward half of the bijection — orphan
      sidecars, the reverse half, are caught store-side by the checker);
    - **S4** the sidecar is valid JSON and validates the schema.

    Messages are repo-root-relative. The checker maps this over every ``.bib``.
    """
    root = Path(root)
    rel = bib_path.relative_to(root)
    messages: list[str] = []

    text = bib_path.read_text(encoding="utf-8")
    try:
        citekey, year, canonical, _ = canonical_entry(text)
    except ValueError as exc:
        # A parse error / multi-entry file fails S4 outright; nothing else to say.
        return [f"{rel}: {exc}"]

    if canonical != text:
        messages.append(
            f"{rel} is not in canonical form (the normalizer would rewrite it)"
        )

    if bib_path.stem != citekey:
        messages.append(
            f"{rel}: filename stem '{bib_path.stem}' != citekey '{citekey}' "
            f"(the filename must equal the citekey — rename to '{citekey}.bib')"
        )

    shard = bib_path.parent.name
    if shard != year:
        messages.append(
            f"{rel} is in entries/{shard}/ but its year field is {year} "
            f"(year-shard mismatch — move it to entries/{year}/)"
        )

    meta_rel = meta_path.relative_to(root)
    if not meta_path.exists():
        messages.append(
            f"{rel} has no sidecar {meta_rel} (bijection broken)"
        )
    else:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            messages.append(f"{meta_rel} is not valid JSON ({exc})")
        else:
            error = sidecar.validation_error(data, str(meta_rel), root)
            if error is not None:
                messages.append(error)

    return messages
