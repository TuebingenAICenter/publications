# `publication-store` tools

The installable CLIs that keep the publication store in canonical form and move data in
and out of it. One package (`publication-store`, see [`pyproject.toml`](pyproject.toml))
pinning `zotero-rdf-python` + `jsonschema`, exposing five `console_scripts`.

For the store layout, the S1–S5 contract, and the contributor workflow, see the
[repository README](../README.md). This file is the operator's guide to the CLIs
themselves: what each one does, how to invoke it, and (for the one that touches GitHub)
which credentials it needs and where to get them.

## Install

```bash
pip install ./tools                 # the CLIs
pip install -e "tools/[test]"       # editable + pytest, for working on the tools
pytest tools/tests                  # the pr1–pr14 fixtures (run manually; not in CI)
```

All commands take `--root` (the store repo root, default: the current directory). Run
them from a checkout of the store, or point `--root` at one.

## Credentials at a glance

Four of the five CLIs are **deterministic and secret-free** — they only read and write
files in your checkout and never reach the network. The lone exception is
`pubstore-publish`, which opens PRs against the store repo on GitHub.

| CLI | Touches GitHub? | Needs a token? |
|---|---|---|
| `pubstore-normalize` | no | no |
| `pubstore-check` | no | no |
| `pubstore-compile` | no | no |
| `pubstore-groups` | no | no |
| `pubstore-publish` | **yes** | **yes** — see [its section](#pubstore-publish) |

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
same invocation runs on the scraper's monthly cron and locally for staging.

Idempotent: it skips any item whose citekey is already in the store *or* whose branch
already exists, so re-runs only file the remaining delta.

```bash
pubstore-publish <export.rdf> --repo OWNER/REPO \
    [--base main] [--limit N] [--dry-run] [--token ...]
```

- `rdf` — the Zotero RDF export (groups carried as collections).
- `--repo` — `OWNER/REPO` of the store, e.g. `TuebingenAICenter/publications` (required).
- `--base` — base branch to open PRs against (default `main`).
- `--limit` — cap the number of PRs *created* this run (skips don't count against it).
- `--dry-run` — compute and log; still queries the remote for the skip checks, but opens
  no PR.

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

**2. Plain token (local / manual staging).** Simpler for a one-off run from your machine:

- `--token <TOKEN>`, or set `GITHUB_TOKEN` in the environment.
- Use a fine-grained personal access token scoped to the store repo with
  **Contents: read & write** and **Pull requests: read & write**, from
  GitHub → *Settings → Developer settings → Personal access tokens*.

If neither form is fully supplied the command exits with an actionable message and does
nothing.
