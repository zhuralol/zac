"""Bootstrap cards from a downloads folder without itch.io metadata.

Useful when packs were downloaded free/elsewhere, or before an API key is set up.
Cards created here use `acquired: "local"` and a `local-<slug>` id; a later
`ingest` adopts them instead of creating duplicates.
"""

from __future__ import annotations

from pathlib import Path

from .cards import LOCAL_ID_PREFIX, load_cards, save_card
from .config import Config
from .licensing import build_attribution
from .util import info, now_iso, slugify
from .taxonomy import is_junk_dir


def import_directory(
    config: Config, downloads: Path | None = None, *, include_empty: bool = False
) -> dict[str, int]:
    assert config.paths is not None
    root = (downloads or config.paths.downloads).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"downloads folder not found: {root}")

    existing = load_cards(config.paths.cards)
    created = skipped = empty = 0

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if is_junk_dir(folder.name):
            continue
        if not include_empty and not any(folder.iterdir()):
            info(f"  skipping empty folder: {folder.name}")
            empty += 1
            continue

        slug = slugify(folder.name)
        if slug in existing:
            skipped += 1
            continue

        title = folder.name
        credit_src = {"title": title, "author": None, "author_url": None}
        card = {
            "slug": slug,
            "itch": {
                "game_id": f"{LOCAL_ID_PREFIX}{slug}",
                "title": title,
                "url": None,
                "author": None,
                "author_url": None,
                "classification": "assets",
                "cover_url": None,
                "published_at": None,
                "created_at": None,
                "acquired": "local",
                "owned_key": False,
                "sources": ["local-import"],
            },
            "description": "",
            "tags": [],
            "attribution": build_attribution(credit_src),
            "local": {"downloaded": False, "path": None, "folder": folder.name},
            "summary": f"{title}: imported from {folder.name}; run `python -m zac scan` to analyze contents.",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        save_card(config, card)
        existing[slug] = card
        created += 1
        info(f"  imported {folder.name} -> {slug}")

    info(f"import-dir: {created} created, {skipped} already present, {empty} empty skipped")
    return {"created": created, "skipped": skipped, "empty": empty}
