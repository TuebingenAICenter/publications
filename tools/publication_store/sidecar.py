"""Shared sidecar schema — the single source of truth for ``meta/**`` shape.

Both the diff job (:mod:`publication_store.diff_job`) and the global checker
(:mod:`publication_store.checker`) validate sidecars against
``schema/sidecar.schema.json`` so "what is a valid sidecar" is defined exactly
once. The schema allows a bare ``{}`` and any combination of the two optional
halves ``zotero`` / ``custom`` (each an object), and — via
``additionalProperties: false`` — rejects a flat / legacy sidecar whose data
sits in top-level fields like ``abstractNote`` (the **S4** json predicate; the
``pr7`` case).

That rejection is what makes ``data.get("zotero", {})`` safe downstream: once a
sidecar validates, an absent half genuinely means "empty", never "data hiding
under an unexpected key". We **reject, never migrate** — a malformed sidecar is
a human fix, not a silent rewrite.

The schema lives at the **repo root** (``<root>/schema/sidecar.schema.json``) and
is resolved relative to ``--root`` so there is exactly one copy and no drift when
the package is pip-installed elsewhere — it is never bundled into the package.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema


def schema_path(root: Path) -> Path:
    """The sidecar schema's path under ``root`` (the public contract, never bundled)."""
    return Path(root) / "schema" / "sidecar.schema.json"


@lru_cache(maxsize=8)
def _validator(schema_path_str: str) -> jsonschema.protocols.Validator:
    schema = json.loads(Path(schema_path_str).read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def validation_error(data: object, source: str, root: Path) -> str | None:
    """Return a one-line error message if ``data`` is not a valid sidecar, else ``None``."""
    validator = _validator(str(schema_path(root).resolve()))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return None
    err = errors[0]
    loc = "/".join(str(p) for p in err.path) or "(root)"
    return f"{source}: {err.message} (at {loc})"


def split_sidecar(data: object, source: str, root: Path) -> tuple[dict, dict]:
    """Validate ``data`` against the schema, return its ``(zotero, custom)`` halves.

    Raises ``ValueError`` on an invalid shape (a flat/legacy sidecar) rather than
    silently reading it as empty. A bare ``{}`` or a missing half is fine and
    defaults to ``{}``.
    """
    message = validation_error(data, source, root)
    if message is not None:
        raise ValueError(message)
    assert isinstance(data, dict)  # guaranteed by the schema's "type": "object"
    return data.get("zotero", {}), data.get("custom", {})
