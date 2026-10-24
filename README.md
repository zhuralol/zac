# ZAC — Zhura's Asset Classifier

Keeps a local inventory of the **art assets you own on itch.io** so a game-dev
LLM knows what you have, what is actually inside each pack, and how each pack
must be credited.

- **Assets only** — games, tools, and soundtracks are filtered out by itch.io's
  `classification` field.
- **Purchases, bundle claims, gifts, and free assets.** Free assets have no
  download key, so they are collected from the collections you configure.
- **Page metadata** — description, tags, author, and cover scraped from each
  asset page (cached with ETags, 1 request/second).
- **Deep local probing** — image dimensions and palette (pixel-art detection),
  audio duration (SFX vs. music), glTF/OBJ mesh counts, fonts, and numbered
  animation sequences. Reads inside `.zip`/`.tar` without extracting.
- **Licensing with evidence** — LICENSE/README files take priority over page
  text; the matched raw span and confidence are stored. Unknown licenses are
  flagged for review instead of guessed.
- **Two LLM-facing outputs** — `ASSETS.md` (inventory + asset cards) and
  `ATTRIBUTION.md` (ready-to-paste credit lines).

Downloads are **manual** — this tool never downloads anything. Download keys are
stored separately in `keys.json` (gitignored) so an auto-downloader can be added
later.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export ITCH_API_KEY=...   # https://itch.io/user/settings/api-keys
```

Requires Python 3.11+. `py7zr` and `fonttools` are optional (7z inspection and
font glyph counts).

## Quick start

```bash
# 1. See your collections and pick the ones that contain assets
python -m zac collections

# 2. Add them to config.toml
#    collections = ["My Game Assets", "Pixel Art"]

# 3. Pull inventory (owned keys + configured collections)
python -m zac ingest

# 4. Scrape descriptions, tags, and license hints
python -m zac enrich

# 5. Download packs manually into downloads/<pack name>/ and scan them
python -m zac scan

# 6. Generate the Markdown your LLM reads
python -m zac render
```

Or run steps 3–6 in one go:

```bash
python -m zac update
```

`update` is incremental: page requests are conditional (ETag), and files are
probed only when their path/size/mtime fingerprint changes.

## CLI reference

| Command | What it does |
|---|---|
| `collections` | list your itch.io collections with IDs and game counts |
| `ingest` | merge owned download keys + configured collections into cards |
| `ingest-url <url>` | add one asset page manually (delisted/private assets) |
| `import-dir [--downloads-dir PATH]` | bootstrap cards from folders without itch metadata/API key |
| `enrich [--force] [--limit N]` | scrape asset pages for description/tags/author |
| `scan [--no-probe] [--force] [--no-interactive]` | match folders and analyze contents |
| `render` | regenerate `ASSETS.md` and `ATTRIBUTION.md` |
| `update` | `ingest` → `enrich` → `scan` → `render` |

Global: `--config PATH` (default `./config.toml`).

Useful flags:

- `--force` — ignore page/probe caches and refetch/reprobe everything.
- `--no-probe` — scan file names and archives only (fast, no image/audio parsing).
- `--no-interactive` — never prompt for ambiguous folder matches; pick the best
  guess and log it.
- `--downloads-dir PATH` — scan a different downloads directory.

## Layout

| Path | Purpose |
|---|---|
| `config.toml` | collections to ingest, classification allowlist, paths |
| `downloads/<pack>/` | manual downloads, one folder per asset (gitignored) |
| `assets/cards/<slug>.json` | canonical per-asset card (source of truth) |
| `assets/matches.json` | remembered folder ↔ asset decisions |
| `keys.json` | download keys (gitignored, never written into cards) |
| `.cache/pages/` | page ETag cache (gitignored) |
| `.cache/scan/probe.json` | file-probe fingerprint cache (gitignored) |
| `ASSETS.md` | LLM-facing inventory and asset cards |
| `ATTRIBUTION.md` | credit lines grouped by license |

## What a card contains

Each `assets/cards/<slug>.json` holds:

```jsonc
{
  "slug": "pixel-necro-pack",
  "itch": {
    "game_id": 123456,
    "title": "Pixel Necro Pack v2",
    "url": "https://pixelpoison.itch.io/pixel-necro-pack",
    "author": "Pixel Poison",
    "author_url": "https://pixelpoison.itch.io",
    "classification": "assets",
    "acquired": "purchased",        // purchased | claimed | free | manual
    "owned_key": true,
    "sources": ["owned-keys", "collection:My Game Assets"]
  },
  "description": "…itch.io description as Markdown…",
  "tags": ["pixel art", "dark fantasy"],
  "attribution": {
    "author": "Pixel Poison",
    "license": {
      "type": "CC BY 4.0",
      "confidence": "high",          // high | medium | none
      "source": "LICENSE.txt",
      "raw_span": "…matched text…"
    },
    "requires_attribution": "yes",   // yes | no | unknown
    "credit_line": "Pixel Necro Pack v2 by Pixel Poison (https://…) — CC BY 4.0"
  },
  "local": {
    "downloaded": true,
    "path": "downloads/pixel-necro-pack-v2",
    "file_count": 4261,
    "total_bytes": 220200960,
    "categories": { "images": { "count": 4211, "bytes": 0, "label": "sprites/textures" } },
    "notable_files": { "images": ["sprites/enemy_idle_01.png"] },
    "manifest": [{ "path": "…", "size": 0, "category": "images" }],
    "animation_sequences": 210,
    "probe": {
      "images": { "pixel_art": 3900, "size_buckets": { "17-32": 3200 } },
      "audio": { "sfx": 38, "music": 12, "total_duration": 1800.0 },
      "models_3d": { "count": 0, "meshes": 0 }
    }
  },
  "summary": "One-line human/LLM summary.",
  "updated_at": "2026-10-24T12:00:00Z"
}
```

The file manifest is capped at 2,000 entries per pack (`manifest_truncated: true`
beyond that) to keep cards manageable.

## How matching works

Folder names are normalized (`Pixel Necro Pack v2` → `pixelnecropack`) and
compared to each card's slug, title, and URL slug. Exact and prefix matches are
automatic; ambiguous ones prompt once and are remembered in
`assets/matches.json`. Edit that file by hand if needed.

Folders with no matching card are reported and left alone.

## Using the output with an LLM

Point your coding assistant at `ASSETS.md` first. It contains the index table and
one compact card per asset, including what the pack contains (counts per
category, pixel-art stats, SFX vs. music), example file paths, the local
directory, license, and the exact credit line. Open
`assets/cards/<slug>.json` when you need the full file manifest or raw license
evidence. `ATTRIBUTION.md` is for the credits screen — copy the lines under each
license type; assets with no detected license are grouped under **needs review**.

### Agent skill (Claude / OpenCode)

`skills/zac-assets/` is a portable skill that teaches a coding agent to query the
inventory before writing game code, avoid treating metadata-only packs as
downloaded, apply license restrictions, and emit correct credits. It bundles
`scripts/find_assets.py`:

```bash
python skills/zac-assets/scripts/find_assets.py --tag pixel-art --category images --downloaded
python skills/zac-assets/scripts/find_assets.py --license "CC BY" --credits
python skills/zac-assets/scripts/find_assets.py --json "tileset"
```

Install it for both harnesses by linking the folder:

```bash
ln -s "$PWD/skills/zac-assets" ~/.claude/skills/zac-assets
ln -s "$PWD/skills/zac-assets" ~/.config/opencode/skills/zac-assets
```

Set `ZAC_ROOT=/path/to/checkout` if the inventory is not in the working directory
or `~/Projects/zac`.

## Delisted / private assets

`/profile/owned-keys` only returns published games. For anything missing:

```bash
python -m zac ingest-url https://author.itch.io/pack-name
```

The card is created from the public page and marked `acquired: "manual"`.

If you have packs on disk but no API key (or they came from elsewhere), bootstrap
cards from the folder structure and let `scan` discover licenses from the files:

```bash
python -m zac import-dir --downloads-dir /path/to/downloads
python -m zac scan --downloads-dir /path/to/downloads --no-interactive
python -m zac render
```

A later `ingest` adopts these local cards by matching title/slug, so no
duplicates are created.

## Tests

Offline tests (mock itch.io API, synthetic images/audio, no network):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Coverage includes ingest filtering/merging and auth failure, license priority and
CC version parsing, page parsing/sanitization, a full scan → render round-trip,
and probe-cache idempotency.

## Notes and limits

- 7z archives are listed but not deeply probed unless `py7zr` is installed; RAR
  is not inspected.
- `.fbx`/`.blend` binaries are inventoried but not parsed.
- Pixel-art detection is a heuristic (limited palette + modest dimensions).
- License detection is heuristic: `confidence` and `raw_span` are recorded, and
  anything unspecified is listed under **needs review** in `ATTRIBUTION.md`.
  Always verify before shipping.
- itch.io page markup can change; `enrich` failures degrade to "metadata missing"
  rather than aborting the run.
