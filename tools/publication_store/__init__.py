"""Publication store tooling.

Two deterministic, secret-free CLIs that keep the institute's publication store
in canonical form, sharing one per-entry core:

- ``pubstore-normalize`` (:mod:`publication_store.diff_job`) — the per-MR diff
  job: normalize the changed ``.bib`` files into the store layout (mutating).
- ``pubstore-check`` (:mod:`publication_store.checker`) — the full-store gate:
  verify the store invariants S1–S5 across every entry (read-only).

The invariants S1–S5 and the two-tool design are specified in the publication
store plan; the per-file predicates live in :mod:`publication_store.entry` and
the sidecar contract in :mod:`publication_store.sidecar`.
"""

__all__ = ["entry", "sidecar", "diff_job", "checker"]
