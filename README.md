# Publications

The Tübingen AI Center's publication **ground-truth store**: the institute's
publications held as data, in a canonical form, plus the tooling to submit and view
them. The source of truth is **one BibTeX file per publication**
(`entries/<year>/<citekey>.bib`) with a **per-entry JSON sidecar**
(`meta/<year>/<citekey>.json`) for the metadata BibTeX can't carry.

This repo's job is exactly to **keep the data in the correct form and provide tooling
around submitting and viewing it.** It is *not* a deduplication, scraping, or enrichment
system — it never reasons about whether two entries are the same publication, fetches
sources, or fills in fields. Those are external agents, and they interact with the store
the same way a human does: **by opening a merge request.**

## Where the data lives

```
entries/<year>/<citekey>.bib    # SOURCE OF TRUTH: one publication per file, canonical
meta/<year>/<citekey>.json      # one sidecar per entry; {} | {zotero} | {custom}
groups/<slug>/                  # group inbox: drop a .bib here to assign it to a group
schema/sidecar.schema.json      # the sidecar contract (single source of truth for its shape)
tools/                          # installable package: the CLIs + pinned deps
.github/workflows/              # CI: the diff job (normalize) + the read-only checker (gate)
```

`<year>` is derived from the publication's date (or `undated`); the filename stem is the
entry's citekey. A sidecar has two optional top-level halves and nothing else: **`zotero`**
(the lossless overlay of fields BibTeX can't represent — the `.bib` plus this half
reconstruct the original item) and **`custom`** (the open report layer: status,
provenance, review, group ownership, …). A bare `{}` is the valid empty sidecar. See
[`schema/sidecar.schema.json`](schema/sidecar.schema.json) for the exact contract.

A compiled `all.bib` and per-group `<group>.bib` views are **optional build artifacts** —
emitted on demand, **never committed**. The per-file `entries/`/`meta/` pairs are the only
source.

## How to contribute (manual)

Open a merge request with a `.bib` — in **any** form. It can be raw, loosely placed at the
repo root, with a filename that doesn't match the citekey, or even several entries pasted
into one file. **You do not need to know the directory layout, the citekey scheme, or
JSON.**

The **diff job** runs on your MR and puts the contribution in canonical form for you:
it canonicalizes the BibTeX, derives the correct `entries/<year>/<citekey>.bib` path and
places it there, creates the matching sidecar, removes the stray drop, and commits the
result back to your MR branch. A reviewer then **accepts and merges** into `main` —
merging is the approval.

### Assign a paper to a group

Drop a `.bib` into **`groups/<slug>/`** (e.g. `groups/bethge/`) and the bot files the
paper *and* records the group for you: it places the entry canonically under
`entries/<year>/<citekey>.bib` exactly as above, **unions** `<slug>` into the entry's
`custom.groups`, and deletes your drop. No JSON, no git layout, no citekey scheme — just
the directory name. The committed `groups/<slug>/` folders are the menu of known slugs;
pick from them (creating a brand-new folder is a more visible act a reviewer will catch).

Three outcomes, by what your drop contains:

- **A new paper** → a fresh entry is created and added to the group.
- **An existing paper, unchanged** (drop a copy of one already in the store) → it is just
  **added to the group** — the stored entry is left untouched. This is how you move an
  existing paper into another group: drop it again under the new folder.
- **An existing paper with edits** → the drop is **rejected**. A group drop is never a
  backdoor edit: to change a stored entry, edit `entries/<year>/<citekey>.bib` directly;
  to only add a group, drop the paper unchanged.

Dropping the *same* paper into several `groups/<slug>/` folders in one MR assigns it to
all of them at once (a bulk paste into one folder assigns that group to every entry in it).

`custom.groups` in the sidecar is the **single source of truth** for membership; the
`groups/` folders are an input convenience. To *remove* a group, edit the sidecar — don't
expect deleting a file to do it.

### Check it locally first (optional)

The same logic ships as plain CLIs, so you can run them before opening the MR:

```bash
pip install ./tools
pubstore-normalize --root . <your.bib>   # put your .bib in canonical form, placed in the store
pubstore-check --root .                  # verify the store satisfies S1–S5
```

Both are deterministic, secret-free, and pure-Python.

## How the agents interact

External agents use the **same merge-request interface** as a human — there is no special
path:

- The **scraper agent** opens MRs with newly found publications (normally already canonical,
  since it runs the same conversion locally).
- The **deduplication agent** finds likely duplicates and opens MRs proposing a merged entry
  plus deletion of the redundant ones.

In both cases the diff job normalizes/verifies as usual and **a human merges (or closes)**
the MR. Machine proposes, human disposes.

## The store contract (S1–S5)

Every commit on `main` satisfies five static invariants:

- **S1 — Placement.** Each entry's pair sits at its derived path
  `entries/<year>/<citekey>.bib` + `meta/<year>/<citekey>.json` (`<year>` from the parsed
  item's date or `undated`; stem == citekey).
- **S2 — Uniqueness.** Citekeys are globally unique across the store.
- **S3 — Pairing.** Every `.bib` has exactly one `.json` at the same stem, and vice versa
  (the content may be `{}`, but the file exists).
- **S4 — Well-formed.** Each `.bib` is at the formatter fixpoint; each `.json` validates
  `schema/sidecar.schema.json` (closed top level: `{}` / `zotero` / `custom`, nothing else).
- **S5 — Closure.** The only files under `entries/`/`meta/` are these pairs — no loose
  `.bib` at the root, no orphaned sidecar, no leftover monolith.

`pubstore-check` is the authoritative gate for all five; its module docstring
([`tools/publication_store/checker.py`](tools/publication_store/checker.py)) and
`pubstore-check --help` are the in-repo reference. (Note: S4 checks sidecar *shape* only —
whether a `status` or group field is *correct* is human review, never gated here.)

## Branch protection & merge model

- **PR-only:** no direct pushes to `main`; every change lands through a merge request.
- **Required status:** the read-only **`check`** job (`pubstore-check`, S1–S5) must pass.
  The mutating normalize job is *not* a required status — it repairs, it doesn't gate.
- **Merge when green:** no merge queue and no "require branches up to date." The only thing
  those would additionally catch is the rare concurrent cross-shard citekey collision, which
  is accepted as a transient duplicate and reconciled by the scheduled backstop run of the
  checker (and the dedup agent).
- **Dismiss-stale-reviews-on-push is off**, so the normalizer's commit-back to your branch
  never wipes an existing approval.
