"""One-off backfill: add the paper's PDF/link line to already-open store PRs.

**Run once, by hand — not wired into any workflow.** The forward fix lives in
:func:`publication_store.publish.build_pr_body`, so every PR opened from now on already
carries the line; this script only updates PRs that were opened *before* that change.

It regenerates each open PR's body with the *same* renderer the publisher uses
(:func:`~publication_store.publish.build_pr_body`), fetching the entry's own committed
pair from the PR head so the body reflects what the PR actually contains. The body is
replaced wholesale, so the result is identical to what the publisher would have opened
for that PR — except that reviewer additions (an edited body, ticked checkboxes) are not
preserved. That is why this is deliberate, manual, and defaults to a dry run.

Scope and safety:

* Only PRs whose head branch looks like a publisher branch for the base branch — a bare
  ``<citekey>``, ``update/<citekey>``, or ``rename/<citekey>`` — are considered; bot
  branches such as ``codex/…`` are skipped by default (see ``--include``).
* The pair is read from the PR **head**, and the body is only rewritten when the
  regenerated text actually differs, so a re-run is a no-op.
* ``--dry-run`` (the default) prints what would change and touches nothing. Pass
  ``--apply`` to write.

Auth is the same as the other remote CLIs (App creds win over a plain token):

    PUBBOT_APP_ID / PUBBOT_PRIVATE_KEY / PUBBOT_INSTALLATION_ID   (preferred)
    GITHUB_TOKEN                                                  (fallback)

Usage::

    # preview one PR (prints the proposed body; writes nothing)
    uv run --project tools --extra publish python -m publication_store.backfill_pr_bodies \
        --repo TuebingenAICenter/publications --pr 1184

    # all open PRs — dry run, then --apply
    uv run --project tools --extra publish python -m publication_store.backfill_pr_bodies \
        --repo TuebingenAICenter/publications
    uv run --project tools --extra publish python -m publication_store.backfill_pr_bodies \
        --repo TuebingenAICenter/publications --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import publish
from .zotero_bridge import StoreEntry

#: ``<citekey>`` | ``update/<citekey>`` | ``rename/<citekey>`` — the branches
#: :func:`publication_store.publish.publish_entries` opens. Citekeys are
#: ``[a-z0-9_-]``, so this is anchored and intentionally conservative: a branch that
#: does not match is left alone rather than guessed at.
_PUBLISH_BRANCH_RE = re.compile(r"^(?:update/|rename/)?[a-z0-9_-]+$")


def _action_for_branch(branch: str) -> str:
    """The PR ``action`` (``New`` / ``Update`` / ``Rename``) implied by its branch."""
    if branch.startswith("rename/"):
        return "Rename"
    if branch.startswith("update/"):
        return "Update"
    return "New"


def _head_pair(repo, pr) -> tuple[StoreEntry, publish.EmittedPair] | None:
    """The ``(StoreEntry, EmittedPair)`` a PR actually carries, read off its head.

    Reconstructs a :class:`StoreEntry` by re-parsing the PR head's ``.bib`` and its
    sidecar, so ``build_pr_body`` sees exactly the entry a reviewer sees. Returns
    ``None`` when the PR's diff does not look like one entry pair (e.g. a tooling PR).
    """
    files = [f for f in pr.get_files() if f.status != "removed"]
    bib_files = [f for f in files if f.filename.endswith(".bib")]
    if len(bib_files) != 1:
        return None
    bib_path = bib_files[0].filename
    # Derive the sidecar path from the bib's own placement (the store invariant), so a
    # rename PR (new pair + deleted old) still resolves its own sidecar.
    year = bib_path.split("/")[1] if "/" in bib_path else "undated"
    stem = bib_path.rsplit("/", 1)[-1][: -len(".bib")]
    meta_path = f"meta/{year}/{stem}.json"

    head_sha = pr.head.sha
    bib_text = repo.get_contents(bib_path, ref=head_sha).decoded_content.decode("utf-8")
    sidecar_text = repo.get_contents(meta_path, ref=head_sha).decoded_content.decode("utf-8")

    entry = StoreEntry(citekey=stem, bib=bib_text, sidecar=json.loads(sidecar_text))
    return entry, publish.emit.emit_pair(entry)


def _stored_pair(repo, pair: publish.EmittedPair, base_branch: str) -> tuple[str, str] | None:
    """The ``(bib, meta)`` currently stored on the base branch for this entry, if any.

    Needed so an *Update* PR's regenerated body keeps its "What changed" diff: without
    the pre-change pair the diff section would silently vanish from the rewritten body.
    """
    try:
        bib = repo.get_contents(pair.bib_path.as_posix(), ref=base_branch)
        meta = repo.get_contents(pair.meta_path.as_posix(), ref=base_branch)
    except Exception:
        return None
    return (
        bib.decoded_content.decode("utf-8"),
        meta.decoded_content.decode("utf-8"),
    )


def _citekey_of(path: str) -> str:
    """The citekey (filename stem) of a store ``.bib`` path."""
    return path.rsplit("/", 1)[-1][: -len(".bib")]


def _rename_target(pr) -> str | None:
    """The superseded citekey a rename PR deletes, from its own file list, else ``None``.

    GitHub reports the removed pair either as ``removed`` files or — when the new entry
    is similar enough — as a single ``renamed`` file carrying ``previous_filename``, so
    both shapes are handled.
    """
    for f in pr.get_files():
        if not f.filename.endswith(".bib") and not (f.previous_filename or "").endswith(".bib"):
            continue
        if f.status == "removed" and f.filename.endswith(".bib"):
            return _citekey_of(f.filename)
        if f.status == "renamed" and (f.previous_filename or "").endswith(".bib"):
            return _citekey_of(f.previous_filename)
    return None


def plan_updates(
    repo,
    base_branch: str,
    *,
    include: re.Pattern | None = None,
    limit: int | None = None,
    pr_number: int | None = None,
):
    """Yield ``(pr, new_body, old_body)`` for each open PR the backfill would rewrite.

    Pure planning: no writes. A PR is skipped when its branch is not a publisher branch,
    when its diff is not a single entry pair, or when the regenerated body is identical.
    ``limit`` caps how many *updates* are yielded (for a cautious first pass), not how
    many PRs are inspected. ``pr_number`` restricts the whole run to one PR — the way to
    eyeball a single body before touching the rest (see the ``--pr`` flag).
    """
    if pr_number is not None:
        pulls = [repo.get_pull(pr_number)]
    else:
        pulls = repo.get_pulls(state="open", base=base_branch)

    seen: set[int] = set()  # PRs are created while we page, so a PR can appear twice
    emitted = 0
    for pr in pulls:
        if pr.number in seen:
            continue
        seen.add(pr.number)
        branch = pr.head.ref
        if not _PUBLISH_BRANCH_RE.match(branch):
            continue
        if include is not None and not include.search(branch):
            continue
        try:
            loaded = _head_pair(repo, pr)
        except Exception as exc:  # one unreadable PR must not sink the run
            print(f"  #{pr.number} {branch}: skipped ({exc})")
            continue
        if loaded is None:
            print(f"  #{pr.number} {branch}: skipped (not a single entry pair)")
            continue
        entry, pair = loaded
        action = _action_for_branch(branch)
        # A rename PR's body names the superseded citekey; recover it from the PR's
        # deleted files (the entry's own marker is stripped before it is stored).
        replaces = None
        if action == "Rename":
            replaces = _rename_target(pr)
            if not replaces:
                print(f"  #{pr.number} {branch}: skipped (rename target unknown)")
                continue
        new_body = publish.build_pr_body(
            entry,
            pair,
            action=action,
            replaces=replaces,
            old_pair=_stored_pair(repo, pair, base_branch) if action == "Update" else None,
        )
        if new_body != (pr.body or ""):
            yield pr, new_body, pr.body or ""
            emitted += 1
            if limit is not None and emitted >= limit:
                return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="OWNER/REPO of the store repository")
    ap.add_argument("--base", default="main", help="base branch the PRs target (default: main)")
    ap.add_argument(
        "--apply", action="store_true",
        help="actually edit the PR bodies (default: dry run, prints only)",
    )
    ap.add_argument(
        "--include", default=None,
        help="only consider head branches matching this regex (default: all publisher branches)",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="stop after rewriting this many PR bodies (for a cautious first pass)",
    )
    ap.add_argument(
        "--pr", type=int, default=None, dest="pr_number",
        help="only look at this one PR number (dry-run it to preview a single body)",
    )
    args = ap.parse_args()

    from ._github import github_repo

    repo = github_repo(
        args.repo,
        token=os.environ.get("GITHUB_TOKEN"),
        app_id=os.environ.get("PUBBOT_APP_ID"),
        private_key=os.environ.get("PUBBOT_PRIVATE_KEY"),
        installation_id=os.environ.get("PUBBOT_INSTALLATION_ID"),
    )
    include = re.compile(args.include) if args.include else None

    changed = 0
    for pr, new_body, old_body in plan_updates(
        repo, args.base, include=include, limit=args.limit, pr_number=args.pr_number
    ):
        changed += 1
        if args.apply:
            pr.edit(body=new_body)
            print(f"  #{pr.number} {pr.head.ref}: body updated")
        elif args.pr_number is not None:
            # Single-PR mode is for eyeballing exactly what would be written.
            print(new_body)
        else:
            print(
                f"  #{pr.number} {pr.head.ref}: would update body "
                f"({len(old_body)} → {len(new_body)} chars)"
            )

    verb = "updated" if args.apply else "would update"
    print(f"\n{verb} {changed} PR body/bodies.")
    if not args.apply and changed:
        print("Re-run with --apply to write the changes.")


if __name__ == "__main__":
    main()
