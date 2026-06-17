"""Shared helpers for the manual pr1–pr7 suite.

This suite is a developer harness, run on demand (``pytest tools/tests`` from a venv
with the package installed) — it is **deliberately not wired into any CI**. The store
gate that does run in CI is ``pubstore-check``, not this.

Most cases assert on the pure library functions (:func:`run_diff` wraps
``normalize_changed``; :func:`run_check` wraps ``check_store``) for tree + message
checks. The loud-failure cases (pr5/pr6/pr7) additionally drive the console-script
``main()`` entry points (:func:`run_diff_main` / :func:`run_check_main`) to assert the
exit code and the ``::error::`` annotations a CI run would see.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from publication_store import checker, diff_job, entry, groups

# tools/tests/conftest.py -> tools/tests -> tools -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SRC = REPO_ROOT / "schema" / "sidecar.schema.json"
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(*parts: str) -> str:
    """Read a fixture file's text (e.g. ``load_fixture("pr1", "drop.bib")``)."""
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


def install_schema(root: Path) -> None:
    """Copy the real sidecar schema into ``root/schema/`` (resolved relative to --root)."""
    dst = root / "schema"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCHEMA_SRC, dst / "sidecar.schema.json")


def write_sidecar(meta_path: Path, zotero: dict, custom: dict) -> None:
    """Write a sidecar exactly as the diff job does (sorted, 2-space, trailing newline)."""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"zotero": zotero, "custom": custom}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def make_store(root: Path, items: list[dict]) -> Path:
    """Seed an already-canonical store under ``root`` (installs the schema too).

    Each ``item`` is ``{"bib": <raw bib text>, "zotero"?: dict, "custom"?: dict}``. The
    bib is run through ``canonical_entries`` (so the seed sits at the S1/S4 fixpoint and
    its derived placement), and the sidecar is written byte-for-byte the way the diff job
    would — guaranteeing the seed satisfies S1–S5 and that re-normalizing it is a true
    no-op. ``items=[]`` just installs the schema (an empty store for the drop cases).
    """
    install_schema(root)
    for item in items:
        bib_raw = item["bib"]
        zotero_in = item.get("zotero") or {}
        custom_in = item.get("custom") or {}
        ck0 = entry.citekey_of(bib_raw)
        sidecar_map = {ck0: zotero_in} if zotero_in else None
        targets = entry.canonical_entries(bib_raw, sidecar_map)
        assert len(targets) == 1, "make_store seeds one entry per item"
        citekey, year, text, spill = targets[0]
        bib_rel, meta_rel = entry.derive_path(year, citekey)
        bib_path = root / bib_rel
        bib_path.parent.mkdir(parents=True, exist_ok=True)
        bib_path.write_text(text, encoding="utf-8")
        write_sidecar(root / meta_rel, spill, custom_in)
    return root


def run_diff(root: Path, paths: list[Path]):
    """Run the diff job's pure core; returns ``(written, removed, warnings)``."""
    return diff_job.normalize_changed(root, [Path(p) for p in paths])


def run_check(root: Path) -> list[str]:
    """Run the full-store checker's pure core; returns the error-message list ([] == OK)."""
    return checker.check_store(root)


def drop_into_group(root: Path, slug: str, filename: str, text: str) -> Path:
    """Write a raw ``groups/<slug>/<filename>`` drop (the Part A inbox); returns its path."""
    drop = root / "groups" / slug / filename
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_text(text, encoding="utf-8")
    return drop


def groups_of(root: Path, year: str, citekey: str) -> list[str]:
    """Read ``custom.groups`` from a stored entry's sidecar (``[]`` if absent)."""
    _, meta_rel = entry.derive_path(year, citekey)
    data = json.loads((root / meta_rel).read_text(encoding="utf-8"))
    return data.get("custom", {}).get("groups", [])


def run_associate(root: Path, paths: list[Path]):
    """Run the group add-on's pure core; returns ``(written, removed, messages)``."""
    return groups.associate(root, [Path(p) for p in paths])


def run_groups_main(root: Path, paths: list[Path], monkeypatch, capsys) -> tuple[int, str]:
    """Drive ``pubstore-groups associate`` via ``main()``; return ``(exit_code, stdout)``."""
    argv = ["pubstore-groups", "associate", *[str(p) for p in paths], "--root", str(root)]
    return _run_main(groups, argv, monkeypatch, capsys)


def _run_main(module, argv: list[str], monkeypatch, capsys) -> tuple[int, str]:
    """Drive a console-script ``main()`` in-process; return ``(exit_code, stdout)``."""
    monkeypatch.setattr(sys, "argv", argv)
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, capsys.readouterr().out


def run_diff_main(root: Path, paths: list[Path], monkeypatch, capsys) -> tuple[int, str]:
    """Drive ``pubstore-normalize`` via ``main()``; return ``(exit_code, stdout)``."""
    argv = ["pubstore-normalize", *[str(p) for p in paths], "--root", str(root)]
    return _run_main(diff_job, argv, monkeypatch, capsys)


def run_check_main(root: Path, monkeypatch, capsys) -> tuple[int, str]:
    """Drive ``pubstore-check`` via ``main()``; return ``(exit_code, stdout)``."""
    argv = ["pubstore-check", "--root", str(root)]
    return _run_main(checker, argv, monkeypatch, capsys)


@pytest.fixture
def helpers():
    """Bundle the helpers so tests can grab them off one fixture if they prefer."""
    return _Helpers


class _Helpers:
    load_fixture = staticmethod(load_fixture)
    install_schema = staticmethod(install_schema)
    write_sidecar = staticmethod(write_sidecar)
    make_store = staticmethod(make_store)
    run_diff = staticmethod(run_diff)
    run_check = staticmethod(run_check)
    run_diff_main = staticmethod(run_diff_main)
    run_check_main = staticmethod(run_check_main)
    drop_into_group = staticmethod(drop_into_group)
    groups_of = staticmethod(groups_of)
    run_associate = staticmethod(run_associate)
    run_groups_main = staticmethod(run_groups_main)
