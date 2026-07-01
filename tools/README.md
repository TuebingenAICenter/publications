# `publication-store` tools

The installable CLIs that keep the publication store in canonical form and move data in
and out of it. One package (`publication-store`, see [`pyproject.toml`](pyproject.toml))
pinning `zotero-rdf-python` + `jsonschema`, exposing six `console_scripts`.

For the store layout, the S1–S5 contract, and the contributor workflow, see the
[repository README](../README.md). This file is the operator's guide to the CLIs
themselves: what each one does, how to invoke it, and (for the one that touches GitHub)
which credentials it needs and where to get them.

## Install

```bash
pip install ./tools                 # the deterministic CLIs (no GitHub deps)
pip install "./tools[publish]"      # + PyGithub, for pubstore-{publish,sweep,blacklist}
pip install -e "tools/[test]"       # editable + pytest, for working on the tools
pytest tools/tests                  # the pr1–pr14 fixtures (run manually; not in CI)
```

`pubstore-publish`, `pubstore-sweep`, and `pubstore-blacklist` are the CLIs that need
PyGithub; all live behind the `[publish]` extra so images that only run the
check/compile/normalize jobs stay slim. `pubstore-blacklist` additionally shells out to the
`git` binary for its clone (present on any CI image with a git checkout) — no extra Python
dependency.

All commands take `--root` (the store repo root, default: the current directory). Run
them from a checkout of the store, or point `--root` at one.

## Credentials at a glance

Four of the seven CLIs are **deterministic and secret-free** — they only read and write
files in your checkout and never reach the network. The three exceptions are
`pubstore-publish`, which opens PRs against the store repo on GitHub, `pubstore-sweep`,
which merges/closes them, and `pubstore-blacklist`, which reads the repo's PRs + history.

| CLI | Touches GitHub? | Needs a token? |
|---|---|---|
| `pubstore-normalize` | no | no |
| `pubstore-check` | no | no |
| `pubstore-compile` | no | no |
| `pubstore-groups` | no | no |
| `pubstore-publish` | **yes** | **yes** — see [its section](#pubstore-publish) |
| `pubstore-sweep` | **yes** | **yes** — see [its section](#pubstore-sweep) |
| `pubstore-blacklist` | **yes** (read-only + clone) | **yes** — see [its section](#pubstore-blacklist) |

---

## `pubstore-normalize`

Per-MR diff job (mutating). Rewrites the changed `.bib` files into the canonical store
layout — `entries/<year>/<citekey>.bib` + `meta/<year>/<citekey>.json` — derived from
each entry's own citekey and year, deleting the raw source. Splits multi-entry pastes,
carries existing `custom` sidecar data across relocations, and fails loudly (exit 1,
`::error::` lines) on a citekey collision or a non-idempotent result rather than
overwriting silently. This is what CI runs to canonicalize a contributor's input.

```bash
pubstore-normalize <changed.bib> [<more.bib> ...] [--root .]
```

- `paths` — the changed `.bib` files (the workflow feeds it the `git diff` list); a
  file may live anywhere in the repo.

## `pubstore-check`

Full-store gate (read-only). Parses every entry and verifies all of S1–S5 (placement,
uniqueness, pairing, well-formed, closure) across the whole store. Exit 0 with an `OK`
line if clean; exit 1 with one `::error::` per violation otherwise. This is the
**required status check** on every PR — run it locally to confirm green before pushing.

```bash
pubstore-check [--root .]
```

## `pubstore-compile`

Add-on (read-only). Compiles the per-entry store into the consumer-facing joined build
artifacts. **Never committed**, always reproducible from `entries/**` + `meta/**`, and
**not a gate**: it assumes S1–S5 already hold (run on a green `main`), warns + skips
anything unparseable, and never fails. The `release.yml` workflow runs this to attach
each view to the snapshot release.

```bash
pubstore-compile [--root .] [--out build/] [--only KIND ...]
```

- `--out` — output directory (default `build/`, created if absent).
- `--only` — emit just one artifact; repeatable. Kinds: `all-bib`, `group-bibs`,
  `meta-json`, `rdf`. Default emits all four.

Outputs: `all.bib` (every entry), `groups/<slug>.bib` (one per PI group),
`meta.json` (the verbatim `{citekey: sidecar}` join), `library.rdf` (the full
Zotero-importable library, with groups as collections + tags).

## `pubstore-groups`

Add-on for the `groups/` directories. Two independent subcommands sharing only the
`groups/` namespace; both treat the sidecar's `custom.groups` as the single source of
truth.

```bash
pubstore-groups associate <groups/<slug>/file.bib> [...] [--root .]
pubstore-groups rebuild-mirror [--check] [--root .]
```

- **`associate`** (per-MR diff job, mutating) — places a `groups/<slug>/*.bib` drop as a
  normal stored entry **and** unions `<slug>` into its `custom.groups`. A new key is
  created; an existing key with identical content is just added to the group; an
  existing key with *different* content is rejected (a group drop is never a backdoor
  edit). The drop is consumed.
- **`rebuild-mirror`** (its own push-to-main workflow) — regenerates the `groups/<slug>/`
  symlink browse mirror from `custom.groups`, wholesale. A derived view; manual edits are
  reverted on the next run. `--check` is read-only: it reports mirror drift and exits 1
  on any (never folded into `pubstore-check`).

## `pubstore-publish`

Producer (mutating, **remote**). Turns a Zotero RDF export into the store's go-forward
ingestion shape: **one branch + one PR per publication** against the store repo, carrying
the full canonical pair. No local clone — it commits via the GitHub Git Data API. The
same invocation runs on the scraper's monthly cron and locally for staging. Install with
the `[publish]` extra (`pip install "./tools[publish]"`) — it's the only CLI that pulls in
PyGithub.

Idempotent. The store's citekey set is read once; per item it either **adds** (citekey
not in the store), **updates** (citekey in the store but the emitted pair differs byte
for byte), **renames** (see below), or **skips** (pair byte-identical, or a branch for
that op already exists). Re-runs only file the remaining delta.

```bash
pubstore-publish <export.rdf> --repo OWNER/REPO \
    [--base main] [--limit N] [--dry-run] [--token ...]
```

- `rdf` — the Zotero RDF export (groups carried as collections).
- `--repo` — `OWNER/REPO` of the store, e.g. `TuebingenAICenter/publications` (required).
- `--base` — base branch to open PRs against (default `main`).
- `--limit` — cap the number of PRs *opened* this run — adds + updates + renames (skips
  don't count against it).
- `--dry-run` — compute and log; still queries the remote for the skip checks, but opens
  no PR.

Each op uses its own branch namespace so concurrent open PRs never collide: adds on
`<citekey>`, updates on `update/<citekey>`, renames on `rename/<citekey>`.

### Identity, updates, and renames

The **citekey is the store identity**. An item's pair is keyed by it, so keeping it stable
across metadata edits is what makes updates (rather than accidental duplicates) work.

- **Pin your citation keys.** With dynamic (unpinned) keys, correcting an author, title,
  or year *recomputes* the citekey — the corrected item then looks like a brand-new add
  and the old entry is silently orphaned. Pin keys in Zotero (Better BibTeX → **Pin BibTeX
  key**, which writes `Citation Key: <key>` into the item's *extra*); a pinned key survives
  any metadata edit, so a correction becomes a clean in-place **update** PR. `pubstore-publish`
  **warns** (non-blocking) and lists every item whose key was auto-generated, so you can pin
  it.
- **Deliberate renames** (you really do want a new citekey for an existing publication):
  add a line to the item's *extra* in Zotero:

  ```
  Replaces: <old_citekey>
  ```

  On publish, the item opens a `rename/<new_citekey>` PR that writes the new pair **and
  deletes the superseded `<old_citekey>` pair in the same commit**; the PR body names what
  it supersedes. The marker is operational only — it is stripped from the stored entry. If
  `<old_citekey>` isn't in the store, the run logs a `::warning::` and falls back to a
  normal add/update.

### PR contents and labels

Each PR is self-contained for review without opening the Files tab. The body carries an
inline header (title, authors, type, groups, citekey), a per-action review checklist, the
BibTeX in a collapsible block, and — on an **update** — a collapsible diff of the `.bib`
and sidecar so the reviewer sees exactly what changed.

Every PR also gets filter **labels** (created on the repo if missing, hence the
**Issues: write** permission above): the op kind (`new` / `update` / `rename`),
`type:<itemType>`, and one `group:<slug>` per owning group.

### Duplicate hints

`pubstore-publish` does **not** compute similarity itself — that keeps `publib` (and its
dependency weight) out of this tool and the CI image. Instead it *renders* candidates an
upstream similarity scan supplies via a `Possible-Duplicates:` *extra* line on the item:

```
Possible-Duplicates: <citekey>@<score>, <citekey>@<score>
```

(`@<score>` is optional.) The scan — which owns `publib` (e.g. in the scraper) — writes
this marker before export; on publish, the candidates surface as a prominent **"Possible
duplicates already in the store"** warning in the PR body and an extra review-checklist
item (no label — the hint lives in the body only). Like `Replaces:`, the marker is
operational only and is stripped from the stored entry.

### Tokens — what it needs and where to find them

Authentication is resolved from the environment (no flags needed in production). It
accepts two forms; **the GitHub App takes precedence** if all three App vars are set,
otherwise it falls back to a plain token:

**1. GitHub App installation (production — the scraper's cron).** Set all three:

| Env var | What it is | Where to find it |
|---|---|---|
| `PUBBOT_APP_ID` | The `tueai-publications-bot` App's numeric ID | Org settings → **Developer settings → GitHub Apps → tueai-publications-bot** → *About* (App ID). Same App as the workflows' `secrets.APP_ID`. |
| `PUBBOT_PRIVATE_KEY` | A PEM private key for that App | The same App page → *Private keys* → **Generate a private key** (downloads a `.pem`). Pass the PEM **contents**. Stored as the workflows' `secrets.APP_PRIVATE_KEY`. |
| `PUBBOT_INSTALLATION_ID` | The App's installation ID on the store repo's org | The App's **Install App** page → click the org's gear; the number at the end of the `.../installations/<id>` URL. (The workflows mint this implicitly; the cron needs it explicitly.) |

Store these as secrets in the repo that runs the job (the scraper,
`tueai_publication_scraping`), not in source.

The App installation must grant **Contents: read & write**, **Pull requests: read &
write**, *and* **Issues: read & write** on the store repo. The first two cover the
branch/commit and the `POST .../pulls`; **Issues: write** is needed because PR labels
(and creating any missing label) go through the issues API. Missing **Pull requests**
surfaces as `403 Resource not accessible by integration` on the `POST .../pulls` step
*after* the branch/commit already landed (so it leaves an orphan branch); missing
**Issues** surfaces as the same `403` on the label step *after* the PR is open (the PR
survives, just unlabeled). Add the permission in the App settings, approve the update on
the installation, then delete any orphan branches before re-running. Verify with
`gh api /repos/OWNER/REPO/installation --jq '.permissions'`.

**2. Plain token (local / manual staging).** Simpler for a one-off run from your machine:

- `--token <TOKEN>`, or set `GITHUB_TOKEN` in the environment.
- Use a fine-grained personal access token scoped to the store repo with
  **Contents: read & write**, **Pull requests: read & write**, and **Issues: read &
  write** (for PR labels), from
  GitHub → *Settings → Developer settings → Personal access tokens*.

If neither form is fully supplied the command exits with an actionable message and does
nothing.

---

## `pubstore-sweep`

Reviewer (mutating, **remote**). The companion to `pubstore-publish`: it works the backlog
of per-item PRs that the monthly cron leaves open. For publication PRs older than a
threshold it applies one **required** policy — `--on-expiry accept` squash-merges the
stale-but-green ones, `--on-expiry reject` closes them — so the per-item PR volume doesn't
pile up unbounded. No local clone; it acts over the GitHub API. Install with the
`[publish]` extra (it shares PyGithub with `pubstore-publish`).

```bash
pubstore-sweep --repo OWNER/REPO --on-expiry {accept,reject} \
    [--older-than 30d] [--label new --label update ...] \
    [--limit N] [--dry-run] [--token ...]
```

- `--repo` — `OWNER/REPO` of the store (required).
- `--on-expiry` — **required**, no default; the run must state its intent (`accept` or
  `reject`). Run two crons with different policies/ages if you ever need both.
- `--older-than` — age threshold, measured from the PR's **creation** time. Accepts `30d` /
  `2w` / `12h` / a bare integer (days). Default `30d`, matching the monthly publish cadence.
- `--label` — repeatable; the op-kind label(s) that mark a PR as a publication PR (default
  `new`, `update`, `rename`). Only PRs carrying one of these are ever touched.
- `--limit` — cap the number of PRs *acted on* this run (merges + closes); skips and
  not-yet-expired PRs don't count, so a re-run continues through the rest.
- `--dry-run` — report the expired PRs and the action each *would* get, change nothing.
  This is also the **fetch/list** mode for seeing the backlog.

### Accept never forces a merge

On `--on-expiry accept`, only a **cleanly mergeable** expired PR is merged. One that is
expired but failing checks, conflicted, or behind base is **skipped and reported** (a
`::warning::` with the reason) and left open for a human — the sweep never overrides a
red PR. Merges are **squash** (each store PR is a single commit) and the head branch is
deleted afterward.

### Reject

On `--on-expiry reject`, each expired PR gets a short comment explaining the timeout, is
closed, and its head branch is deleted. Deleting the branch matters: it lets a later
`pubstore-publish` of the same citekey re-open the PR cleanly (publish skips a citekey
whose branch still exists).

### Tokens

Identical to [`pubstore-publish`](#tokens--what-it-needs-and-where-to-find-them) — the same
App-installation env vars (`PUBBOT_APP_ID` / `PUBBOT_PRIVATE_KEY` / `PUBBOT_INSTALLATION_ID`)
take precedence, else a plain `--token` / `GITHUB_TOKEN`. The same three permissions are
required: **Contents: read & write**, **Pull requests: read & write** (merge/close and the
branch delete), and **Issues: read & write** (the rejection comment goes through the issues
API). If neither credential form is fully supplied the command exits with an actionable
message and does nothing.

The recurring run is a cron in the scraper repo (`tueai_publication_scraping`), mirroring
the publish cron: `pip install "publication-store[publish]"`, then
`pubstore-sweep --repo TuebingenAICenter/publications --on-expiry accept`. It also runs
locally/manually with a `--token`.

## `pubstore-blacklist`

Producer (**read-only**, remote + clone). Builds `blacklist.rdf`: the items a human has
already *adjudicated away*, so the scraper's incremental dedup
(`publib.new_publications(scraped, store, blacklist)`) stops re-proposing them under a
drifted citekey that `pubstore-publish`'s exact-citekey skip can't catch. It writes only the
RDF — it never merges, closes, or comments. Install with the `[publish]` extra (shares
PyGithub); it also shells out to the `git` binary for a blobless clone (deletion detection
needs history, which the no-clone API path can't reach).

```bash
pubstore-blacklist --repo OWNER/REPO [--out blacklist.rdf] \
    [--label new ...] [--since-year YYYY] [--include path.bib ...] \
    [--dry-run] [--token ...]
```

- `--repo` — `OWNER/REPO` of the store (required).
- `--out` — output RDF path (default `blacklist.rdf`).
- `--label` — repeatable op-kind label marking a blacklist-eligible PR (default `new`;
  `update` / `rename` presuppose the item is already in the store, which `library.rdf`
  already excludes).
- `--since-year` — drop blacklist items published before this year (undated kept), keeping
  the artifact ~constant-size instead of growing with every closed PR and deletion ever.
- `--include` — repeatable extra local hand-curated `.bib` to fold in (on top of the
  in-repo `blacklist/*.bib`).
- `--dry-run` — print the per-source counts and write nothing.

### The four sources

All are reconstructed identically (`.bib` text → `from_bibtex` → `ZoteroItem`), unioned,
then exported:

1. **open** `new` PRs — in-flight, so a paper with an open PR isn't re-proposed under a
   drifted key;
2. **closed-unmerged** `new` PRs — deliberate rejections. Safe *because* `pubstore-sweep`
   defaults to `--on-expiry accept`: a timed-out PR is **merged**, not closed, so a
   closed-unmerged PR is a real human "NO";
3. **store-file deletions** over git history — the post-merge "undo": a citekey present
   somewhere in history but **absent from `entries/` at HEAD**. Keyed on the citekey being
   gone (not on a blob changing), so an in-place metadata fix, a delete-then-re-add, and a
   rename all correctly stay off the list;
4. **hand-curated** entries — an in-repo `blacklist/*.bib` a human maintains (plus any
   `--include` paths), version-controlled and PR-reviewed alongside the store.

One unparseable bib is reported (a `::warning::`) and skipped, never sinking the run; the
command exits 1 if any parse error occurred. There is deliberately **no self-dedup**:
`new_publications` dedups against the blacklist at consumption time anyway, so cross-source
repeats are harmless.

### Tokens

Identical to [`pubstore-publish`](#tokens--what-it-needs-and-where-to-find-them) — the same
App-installation env vars take precedence, else a plain `--token` / `GITHUB_TOKEN`. It reads
PRs and clones, so **Contents: read** and **Pull requests: read** suffice (no write scope).

The recurring run is a cron in the scraper repo, before `pubstore-publish`:
`pubstore-blacklist --repo TuebingenAICenter/publications --out blacklist.rdf --since-year YYYY`,
then the scraper feeds `blacklist.rdf` to `new_publications` (the `--blacklist-rdf` wiring is
a separate follow-up). It also runs locally/manually with a `--token`.
