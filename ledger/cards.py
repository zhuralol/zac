"""Asset card construction, merging, and human/LLM summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .licensing import build_attribution
from .taxonomy import category_label
from .util import human_bytes, info, now_iso, read_json, slug_from_url, slugify, warn, write_json


def card_path(config: Config, slug: str) -> Path:
    assert config.paths is not None
    return config.paths.cards / f"{slug}.json"


def load_cards(cards_dir: Path) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    if not cards_dir.exists():
        return cards
    for path in sorted(cards_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("slug"):
            cards[data["slug"]] = data
    return cards


def save_card(config: Config, card: dict[str, Any]) -> Path:
    card["updated_at"] = now_iso()
    card.pop("_page_text", None)
    path = card_path(config, card["slug"])
    write_json(path, card)
    return path


def _new_card(record: dict[str, Any], slug: str) -> dict[str, Any]:
    itch = {k: record.get(k) for k in (
        "game_id", "title", "url", "author", "author_url", "classification",
        "cover_url", "published_at", "created_at", "acquired", "owned_key", "sources",
    )}
    return {
        "slug": slug,
        "itch": itch,
        "description": "",
        "tags": [],
        "attribution": {
            "author": itch["author"],
            "author_url": itch["author_url"],
            "published_at": itch["published_at"],
            "license": {
                "type": "unspecified",
                "confidence": "none",
                "source": None,
                "raw_span": None,
            },
            "requires_attribution": "unknown",
            "credit_line": f"{itch['title']} by {itch['author'] or 'unknown author'} — license unspecified",
        },
        "local": {"downloaded": False, "path": None},
        "summary": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def merge_ingest(config: Config, records: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Create/refresh cards from API records, preserving enrich/scan data."""
    assert config.paths is not None
    config.paths.cards.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    by_game_id: dict[str, str] = {}
    for path in sorted(config.paths.cards.glob("*.json")):
        card = read_json(path)
        if not isinstance(card, dict) or not card.get("slug"):
            continue
        existing[card["slug"]] = card
        gid = str((card.get("itch") or {}).get("game_id"))
        if gid:
            by_game_id[gid] = card["slug"]

    created = updated = 0
    for gid, record in sorted(records.items(), key=lambda kv: str(kv[1].get("title") or "")):
        slug = by_game_id.get(gid)
        if slug is None:
            base = slug_from_url(record.get("url") or "") or slugify(record.get("title") or "asset")
            slug = base
            if slug in existing and str((existing[slug].get("itch") or {}).get("game_id")) != gid:
                slug = f"{base}-{gid}"
            card = _new_card(record, slug)
            created += 1
        else:
            card = existing[slug]
            updated += 1

        itch = card.setdefault("itch", {})
        for key in (
            "game_id", "title", "url", "author", "author_url", "classification",
            "cover_url", "published_at", "created_at", "acquired", "owned_key", "sources",
        ):
            if record.get(key) is not None:
                itch[key] = record[key]
        if not card.get("attribution"):
            card["attribution"] = build_attribution(itch).copy()
        card["summary"] = summarize(card)
        save_card(config, card)

    # Cards that no longer appear in the API inventory are kept but flagged.
    stale = [slug for slug, card in existing.items() if str((card.get("itch") or {}).get("game_id")) not in records]
    if stale:
        warn(f"{len(stale)} cards not present in the current inventory (kept): {', '.join(stale[:5])}"
             + (" ..." if len(stale) > 5 else ""))

    info(f"ingest: {created} new cards, {updated} refreshed, {len(stale)} stale")
    return {"created": created, "refreshed": updated, "stale": len(stale)}


def merge_enrich(config: Config, slug: str, parsed: dict[str, Any]) -> None:
    """Apply scraped page metadata to an existing card."""
    path = card_path(config, slug)
    card = read_json(path)
    if not card:
        return
    itch = card.setdefault("itch", {})
    for key in ("title", "author", "author_url", "cover_url"):
        if parsed.get(key):
            itch[key] = parsed[key]
    if parsed.get("description"):
        card["description"] = parsed["description"]
    if parsed.get("tags"):
        card["tags"] = parsed["tags"]
    card["attribution"] = build_attribution(
        itch,
        description=card.get("description") or "",
        page_text=parsed.get("page_text") or "",
        docs=[],
    )
    card["summary"] = summarize(card)
    save_card(config, card)


def summarize(card: dict[str, Any]) -> str:
    itch = card.get("itch") or {}
    tags = card.get("tags") or []
    local = card.get("local") or {}
    author = itch.get("author") or "unknown author"
    title = itch.get("title") or card.get("slug")

    if not local.get("downloaded"):
        tail = f" Tagged: {', '.join(tags[:6])}." if tags else ""
        return f"{title} by {author}. Owned ({itch.get('acquired', 'unknown')}) but not downloaded yet; contents unknown.{tail}"

    cats = local.get("categories") or {}
    probe = local.get("probe") or {}
    images = probe.get("images") or {}
    audio = probe.get("audio") or {}
    models = probe.get("models_3d") or {}

    parts: list[str] = []

    img_count = cats.get("images", {}).get("count", 0)
    if img_count:
        detail = []
        if images.get("pixel_art"):
            detail.append(f"{images['pixel_art']:,} pixel-art")
        buckets = images.get("size_buckets") or {}
        if buckets:
            top = max(buckets.items(), key=lambda kv: kv[1])[0]
            detail.append(f"mostly {top} px")
        if local.get("animation_sequences"):
            detail.append(f"{local['animation_sequences']:,} animation sequence"
                          + ("s" if local["animation_sequences"] != 1 else ""))
        suffix = f" ({', '.join(detail)})" if detail else ""
        parts.append(f"{img_count:,} images{suffix}")

    audio_count = cats.get("audio", {}).get("count", 0)
    if audio_count:
        sfx, music = audio.get("sfx", 0), audio.get("music", 0)
        bits = []
        if sfx:
            bits.append(f"{sfx:,} short SFX")
        if music:
            bits.append(f"{music:,} music tracks")
        if not bits:
            bits.append(f"{audio_count:,} audio files")
        parts.append(", ".join(bits))

    for cat in ("models_3d", "fonts", "vector", "tilemaps", "shaders", "source_projects", "docs"):
        count = cats.get(cat, {}).get("count", 0)
        if count:
            label = category_label(cat)
            extra = ""
            if cat == "models_3d" and models.get("meshes"):
                extra = f", {models['meshes']:,} meshes"
            parts.append(f"{count:,} {label}{extra}")

    if local.get("archive_count"):
        parts.append(f"{local['archive_count']} nested archive(s)")

    total = local.get("file_count", 0)
    size = human_bytes(local.get("total_bytes", 0))
    body = ", ".join(parts) if parts else "no recognized asset files"
    summary = f"{title} by {author}: {total:,} files ({size}) — {body}."
    if tags:
        summary += f" Tags: {', '.join(tags[:8])}."
    return summary
