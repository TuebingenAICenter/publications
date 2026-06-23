# Publications

The Tübingen AI Center's publication **ground-truth store** — the institute's
publications held as canonical data, plus the tooling to submit and view them. Source of
truth is **one BibTeX file per publication** (`entries/<year>/<citekey>.bib`) with a
**per-entry JSON sidecar** (`meta/<year>/<citekey>.json`) for what BibTeX can't carry.

Its only job is to **[keep the data in the correct form](#the-store-contract-s1s5)** — it
does *not* deduplicate, scrape, or enrich. That curation is supported by separate tooling
(an automatic publication-scraper agent, Slack reminder bots, …); human contributors take
part directly. Everyone interacts with the store the same way: **by opening a merge
request.**

## Contributing

Everything lands through a merge request; **merging is the approval.** A bot normalizes
your input on the MR, so you don't need to know the layout, the citekey scheme, or JSON —
just do one of:

- **Merge a bot's MR.** The scraper and dedup agents open MRs already in canonical form.
  Review and merge (or close) — that's it.
- **Drop your `.bib` into your group's folder** — [`groups/<slug>/`](groups/) (e.g.
  `groups/bethge/`). The bot files the paper canonically *and* records the group. Pick an
  existing folder from the list; if you have no group, drop the `.bib` at the repo root
  instead.
- **Edit a file directly** (if you must). Change a `.bib` in `entries/` or a sidecar in
  `meta/` — the bot re-canonicalizes and relocates as needed.

Then open the MR and merge it once the check is green.

If the bot **can't** auto-fix it (malformed BibTeX, a duplicate citekey, a half-moved pair,
a legacy sidecar), the check goes red with a clear message — fix the input and push again.
It never guesses silently.

<details>
<summary>Group-drop details</summary>

A drop into `groups/<slug>/` **unions** `<slug>` into the entry's `custom.groups`
(the single source of truth — folders are just an input convenience). Three outcomes:
**new paper** → created and added; **existing paper, unchanged** → just added to the group
(this is how you move a paper into another group); **existing paper with edits** →
rejected (a group drop is never a backdoor edit — edit `entries/` directly instead).
Dropping into several folders in one MR assigns all those groups at once. To *remove* a
group, edit the sidecar — deleting a folder won't do it.
</details>

<details>
<summary>Check it locally first (optional)</summary>

The same logic ships as plain CLIs:

```bash
pip install ./tools
pubstore-normalize --root . <your.bib>   # canonicalize + place your .bib in the store
pubstore-check --root .                  # verify the store satisfies S1–S5
```
</details>

## Where the data lives

```
entries/<year>/<citekey>.bib    # SOURCE OF TRUTH: one publication per file, canonical
meta/<year>/<citekey>.json      # one sidecar per entry; {} | {zotero} | {custom}
groups/<slug>/                  # group inbox (drop a .bib here) + browse mirror (symlinks)
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

A compiled `all.bib`, per-group `<group>.bib` views, a consumer-facing `meta.json`, and
a full Zotero-importable `library.rdf` (the whole library in one file, with group
collections + tags) are **optional build artifacts** — emitted on demand
(`pubstore-compile`), **never committed**. The `groups/` symlink tree is likewise a derived browse view, rebuilt from
`custom.groups`. The per-file `entries/`/`meta/` pairs are the only source of truth.

## Download the data

Every merge to `main` publishes a snapshot
[release](https://github.com/TuebingenAICenter/publications/releases/latest) with each
compiled view attached as its own asset. These links always resolve to the **latest**
release, so they never go stale — no tag in the URL:

- **[`library.rdf`](https://github.com/TuebingenAICenter/publications/releases/latest/download/library.rdf)**
  — the whole library in one file; import straight into Zotero (groups become
  collections, tags included).
- **[`all.bib`](https://github.com/TuebingenAICenter/publications/releases/latest/download/all.bib)**
  — every entry as one BibTeX file.
- **[`meta.json`](https://github.com/TuebingenAICenter/publications/releases/latest/download/meta.json)**
  — the joined sidecars, keyed by citekey (the `custom` report layer + lossless `zotero`
  overlay).
- **`<group>.bib`** — one BibTeX file per PI group, e.g.
  [`bethge.bib`](https://github.com/TuebingenAICenter/publications/releases/latest/download/bethge.bib),
  [`schoelkopf.bib`](https://github.com/TuebingenAICenter/publications/releases/latest/download/schoelkopf.bib).
  Available per [`groups/<slug>/`](groups/) while that group has entries.

The full list of assets (and older snapshots) is on the
[releases page](https://github.com/TuebingenAICenter/publications/releases).

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

## Maintaining the tooling

For contributors working on the store's tooling rather than its data.

`tools/` is a single installable package (`publication-store`, see
[`tools/pyproject.toml`](tools/pyproject.toml)) pinning `zotero-rdf-python` + `jsonschema`
and exposing the CLIs as `console_scripts`:

| CLI | Role |
|---|---|
| `pubstore-normalize` | Per-MR diff job (mutating): normalize the changed `.bib` files into the store. |
| `pubstore-check` | Full-store gate (read-only): verify S1–S5. The required status check. |
| `pubstore-compile` | Add-on (read-only): emit `all.bib` / `<group>.bib` / `meta.json` / `library.rdf` artifacts. |
| `pubstore-groups` | Add-on: `associate` (per-MR group drop → `custom.groups`) and `rebuild-mirror` (regenerate the `groups/` symlink view). |

The shared per-entry logic lives in `publication_store/entry.py` (S1/S4 for one entry);
the two drivers `diff_job.py` and `checker.py` build on it. Each module's docstring is the
authoritative reference for its invariants.

```bash
pip install -e "tools/[test]"
pytest tools/tests           # the pr1–pr14 fixtures; run manually — deliberately not in CI
```

The workflows under `.github/workflows/`:

- **`pr-checks.yml`** — on every PR: the diff job (normalize, commit-back) routed between
  `pubstore-normalize` and `pubstore-groups associate`, then the required `check` gate.
- **`check-scheduled.yml`** — nightly `pubstore-check` backstop (catches landed cross-shard
  duplicates).
- **`regenerate-mirror.yml`** — on push to `main`: rebuild the `groups/` symlink mirror.
- **`release.yml`** — on push to `main`: compile the build artifacts and mint a snapshot
  release with each file attached as its own asset. **`compile-artifacts.yml`** is the
  reusable compile step it (and the manual **`publish-artifacts.yml`**) build on.
- **`build-ci-image.yml`** — rebuild the prebuilt CI image when `tools/` deps change.

Add-ons (compiled artifacts, group directories) sit *beside* the core and can be toggled
without touching the S1–S5 logic or the gate.
