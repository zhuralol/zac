"""Render LLM-facing Markdown: ASSETS.md and ATTRIBUTION.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cards import load_cards
from .config import Config
from .taxonomy import category_label
from .util import human_bytes, info, now_iso

_NEEDS_REVIEW = "needs review"


def _esc(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _license_of(card: dict[str, Any]) -> tuple[str, str]:
    lic = ((card.get("attribution") or {}).get("license") or {})
    return (lic.get("type") or "unspecified"), (lic.get("confidence") or "none")


def _is_downloaded(card: dict[str, Any]) -> bool:
    return bool((card.get("local") or {}).get("downloaded"))


def _index_table(cards: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Asset | Author | License | Tags | Local | Files | Size |",
        "|---|---|---|---|---|---|---|",
    ]
    for card in cards:
        itch = card.get("itch") or {}
        local = card.get("local") or {}
        lic, conf = _license_of(card)
        warn = " ⚠" if lic == "unspecified" else ""
        tags = ", ".join((card.get("tags") or [])[:5])
        if len(card.get("tags") or []) > 5:
            tags += ", …"
        lines.append(
            "| {title} | {author} | {lic}{warn} | {tags} | {local} | {files} | {size} |".format(
                title=f"[{_esc(itch.get('title') or card.get('slug'))}](#{_anchor(itch.get('title') or card.get('slug'))})",
                author=_esc(itch.get("author") or "?"),
                lic=_esc(lic),
                warn=warn,
                tags=_esc(tags),
                local="yes" if local.get("downloaded") else "no",
                files=f"{local.get('file_count', 0):,}" if local.get("downloaded") else "—",
                size=_esc(local.get("total_human") or "—"),
            )
        )
    return lines


def _anchor(title: str) -> str:
    import re

    slug = re.sub(r"[^\w\- ]", "", title.lower()).strip().replace(" ", "-")
    return slug


def _contents_lines(card: dict[str, Any]) -> list[str]:
    local = card.get("local") or {}
    cats = local.get("categories") or {}
    if not cats:
        return []
    lines = ["| Category | Files | Size |", "|---|---|---|"]
    for cat, info_ in cats.items():
        lines.append(
            f"| {_esc(info_.get('label') or category_label(cat))} | {info_.get('count', 0):,} | {human_bytes(info_.get('bytes', 0))} |"
        )
    return lines


def _probe_lines(card: dict[str, Any]) -> list[str]:
    local = card.get("local") or {}
    probe = local.get("probe") or {}
    if not probe:
        return []
    bits: list[str] = []
    images = probe.get("images") or {}
    if images.get("count"):
        bit = f"{images['count']:,} images"
        if images.get("pixel_art"):
            bit += f" ({images['pixel_art']:,} pixel-art)"
        buckets = images.get("size_buckets") or {}
        if buckets:
            top = sorted(buckets.items(), key=lambda kv: -kv[1])[:3]
            bit += "; sizes " + ", ".join(f"{k}px×{v:,}" for k, v in top)
        if images.get("animated_frames"):
            bit += f"; {images['animated_frames']:,} extra animation frames"
        bits.append(bit)
    audio = probe.get("audio") or {}
    if audio.get("count"):
        bit = f"{audio['count']:,} audio file{'s' if audio['count'] != 1 else ''}"
        roles = []
        if audio.get("sfx"):
            roles.append(f"{audio['sfx']:,} SFX (<3s)")
        if audio.get("music"):
            roles.append(f"{audio['music']:,} music (>30s)")
        if roles:
            bit += " (" + ", ".join(roles) + ")"
        if audio.get("total_duration") and audio["total_duration"] >= 30:
            bit += f", {audio['total_duration'] / 60:.1f} min total"
        bits.append(bit)
    models = probe.get("models_3d") or {}
    if models.get("count"):
        bits.append(f"{models['count']:,} 3D files ({models.get('meshes', 0):,} meshes)")
    if local.get("animation_sequences"):
        n = local["animation_sequences"]
        bits.append(f"{n:,} numbered animation sequence{'s' if n != 1 else ''}")
    if probe.get("unprobed"):
        bits.append(f"{probe['unprobed']:,} files not probed")
    return ["**Probe:** " + " · ".join(bits)] if bits else []


def _notable_lines(card: dict[str, Any]) -> list[str]:
    notable = (card.get("local") or {}).get("notable_files") or {}
    if not notable:
        return []
    lines = ["**Example files:**"]
    for cat, paths in notable.items():
        if paths:
            label = category_label(cat)
            shown = ", ".join(f"`{p}`" for p in paths[:6])
            lines.append(f"- {label}: {shown}")
    return lines


def render_card(card: dict[str, Any]) -> list[str]:
    itch = card.get("itch") or {}
    attr = card.get("attribution") or {}
    lic, conf = _license_of(card)
    title = itch.get("title") or card.get("slug")
    lines = [f"### {title}", ""]

    meta: list[str] = []
    if itch.get("author"):
        author = itch["author"]
        if itch.get("author_url"):
            author = f"[{author}]({itch['author_url']})"
        meta.append(f"**Author:** {author}")
    meta.append(f"**Acquired:** {itch.get('acquired') or 'unknown'}"
                + (" (download key available)" if itch.get("owned_key") else ""))
    lic_note = f"**License:** {lic}"
    if lic != "unspecified":
        lic_note += f" ({conf} confidence"
        if (attr.get("license") or {}).get("source"):
            lic_note += f", source: {attr['license']['source']}"
        lic_note += ")"
    else:
        lic_note += " ⚠ — verify before shipping"
    meta.append(lic_note)
    if card.get("tags"):
        meta.append(f"**Tags:** {', '.join(card['tags'])}")
    if itch.get("url"):
        meta.append(f"**Page:** {itch['url']}")
    lines.append(" · ".join(meta))
    lines.append("")

    local = card.get("local") or {}
    if local.get("downloaded"):
        lines.append(
            f"**Path:** `{local.get('path')}` · **Files:** {local.get('file_count', 0):,} "
            f"({local.get('total_human') or human_bytes(local.get('total_bytes', 0))})"
        )
        lines.append("")
    else:
        lines.append("**Downloaded:** no — metadata only, contents unknown.")
        lines.append("")

    if card.get("summary"):
        lines.append(f"> {card['summary']}")
        lines.append("")

    if (card.get("content") or {}).get("description"):
        lines.append(f"**Looks like:** {card['content']['description']}")
        lines.append("")

    if card.get("description"):
        desc = card["description"].strip()
        lines.append("**itch.io description:**")
        lines.append("")
        lines.append(desc)
        lines.append("")

    contents = _contents_lines(card)
    if contents:
        lines.extend(["**Contents:**", ""])
        lines.extend(contents)
        lines.append("")

    probe = _probe_lines(card)
    if probe:
        lines.extend(probe)
        lines.append("")

    notable = _notable_lines(card)
    if notable:
        lines.extend(notable)
        lines.append("")

    lines.append(f"**Credit:** {attr.get('credit_line') or 'unknown'}")
    lines.append("")
    if (local.get("manifest")):
        lines.append(f"**File catalogue:** [assets/files/{card.get('slug')}.md](assets/files/{card.get('slug')}.md)")
        lines.append("")
    lines.append(f"*Card detail: `assets/cards/{card.get('slug')}.json`*")
    lines.append("")
    return lines


def render_assets_md(config: Config) -> Path:
    assert config.paths is not None
    cards = list(load_cards(config.paths.cards).values())
    cards.sort(key=lambda c: (
        not _is_downloaded(c),
        ((c.get("itch") or {}).get("author") or "").lower(),
        ((c.get("itch") or {}).get("title") or c.get("slug") or "").lower(),
    ))
    downloaded = sum(1 for c in cards if _is_downloaded(c))
    owned = sum(1 for c in cards if (c.get("itch") or {}).get("owned_key"))
    lines = [
        "# Owned itch.io assets",
        "",
        f"Generated {now_iso()} · {len(cards)} asset{'s' if len(cards) != 1 else ''} ({downloaded} downloaded, "
        f"{len(cards) - downloaded} metadata-only) · {owned} with download keys",
        "",
        "This file is the LLM-facing inventory. Each card lists what the pack contains,",
        "how it is licensed, and where it lives locally. Machine-readable detail is in",
        "`assets/cards/<slug>.json`; credit text is in `ATTRIBUTION.md`.",
        "",
        "## Index",
        "",
    ]
    lines.extend(_index_table(cards))
    lines.append("")
    lines.append("## Asset cards")
    lines.append("")
    for card in cards:
        lines.extend(render_card(card))
    path = config.paths.assets_md
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    info(f"wrote {path} ({len(cards)} cards)")
    return path


def render_attribution_md(config: Config) -> Path:
    assert config.paths is not None
    cards = list(load_cards(config.paths.cards).values())
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        lic, _conf = _license_of(card)
        key = lic if lic not in (None, "", "unspecified") else _NEEDS_REVIEW
        groups.setdefault(key, []).append(card)

    lines = [
        "# Asset attribution",
        "",
        f"Generated {now_iso()} · {len(cards)} asset{'s' if len(cards) != 1 else ''}",
        "",
        "Copy a line below into your game's credits. Lines in **needs review** have no",
        "detected license — check the asset page before shipping.",
        "",
    ]
    ordered = sorted(k for k in groups if k != _NEEDS_REVIEW) + ([_NEEDS_REVIEW] if _NEEDS_REVIEW in groups else [])
    for key in ordered:
        items = sorted(
            groups[key],
            key=lambda c: (((c.get("itch") or {}).get("author") or "").lower(),
                           ((c.get("itch") or {}).get("title") or "").lower()),
        )
        heading = "⚠ Needs review (no license detected)" if key == _NEEDS_REVIEW else key
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"*{len(items)} asset(s)*")
        lines.append("")
        for card in items:
            itch = card.get("itch") or {}
            attr = card.get("attribution") or {}
            line = attr.get("credit_line")
            if not line:
                line = f"{itch.get('title')} by {itch.get('author') or 'unknown author'}"
            lines.append(f"- {line}")
            details = []
            lic = attr.get("license") or {}
            if lic.get("source"):
                details.append(f"license source: {lic['source']}")
            if lic.get("confidence") and lic["confidence"] not in ("none",):
                details.append(f"confidence: {lic['confidence']}")
            if not _is_downloaded(card):
                details.append("not downloaded")
            if details:
                lines.append(f"  - _{'; '.join(details)}_")
        lines.append("")
    path = config.paths.attribution_md
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    info(f"wrote {path} ({len(cards)} assets)")
    return path


_CATEGORY_ORDER = [
    "images", "audio", "models_3d", "fonts", "vector", "tilemaps",
    "source_projects", "shaders", "docs", "archives", "other",
]


def _facts_label(entry: dict) -> str:
    facts = entry.get("facts") or {}
    if facts.get("width") and facts.get("height"):
        label = f"{facts['width']}x{facts['height']}"
        if facts.get("frames", 1) > 1:
            label += f", {facts['frames']} frames"
        return label
    if facts.get("duration"):
        return f"{facts['duration']:.1f}s"
    if facts.get("meshes"):
        return f"{facts['meshes']} meshes"
    if facts.get("glyphs"):
        return f"{facts['glyphs']} glyphs"
    return human_bytes(entry.get("size", 0))


def render_files_md(config: Config) -> Path:
    """Write one file-level catalogue per pack under assets/files/."""
    assert config.paths is not None
    out_dir = config.paths.cards.parent / "files"
    out_dir.mkdir(parents=True, exist_ok=True)
    cards = list(load_cards(config.paths.cards).values())
    cards.sort(key=lambda c: ((c.get("itch") or {}).get("title") or c.get("slug") or "").lower())

    written = 0
    for card in cards:
        manifest = (card.get("local") or {}).get("manifest") or []
        if not manifest:
            continue
        itch = card.get("itch") or {}
        lines = [
            f"# {itch.get('title') or card.get('slug')} — file catalogue",
            "",
            f"Generated {now_iso()} · {len(manifest):,} files",
            "",
        ]
        local = card.get("local") or {}
        if local.get("path"):
            lines.append(f"**Path:** `{local['path']}`")
        credit = (card.get("attribution") or {}).get("credit_line")
        if credit:
            lines.append(f"**Credit:** {credit}")
        lines.append("")

        buckets: dict[str, list[dict]] = {}
        for entry in manifest:
            buckets.setdefault(entry.get("category", "other"), []).append(entry)

        for category in _CATEGORY_ORDER:
            entries = buckets.pop(category, None)
            if not entries:
                continue
            lines.append(f"## {category_label(category)} ({len(entries):,})")
            lines.append("")
            for entry in sorted(entries, key=lambda e: e["path"]):
                description = entry.get("description") or "no description yet"
                lines.append(f"- `{entry['path']}` ({_facts_label(entry)}) — {description}")
                sheet = entry.get("sheet") or {}
                grid = sheet.get("grid")
                if grid:
                    lines.append(
                        f"  - sheet: {grid['cols']}x{grid['rows']} grid, "
                        f"{grid['cell_w']}x{grid['cell_h']} px cells, {sheet.get('kind') or 'unknown'}"
                    )
                    for cell in sheet.get("cells") or []:
                        desc = cell.get("description")
                        if not desc:
                            continue
                        lines.append(
                            f"  - cell ({cell['row']},{cell['col']}) "
                            f"x={cell['x']} y={cell['y']} w={cell['w']} h={cell['h']} — {desc}"
                        )
            lines.append("")
        for category, entries in buckets.items():
            lines.append(f"## {category_label(category)} ({len(entries):,})")
            lines.append("")
            for entry in sorted(entries, key=lambda e: e["path"]):
                lines.append(f"- `{entry['path']}` ({_facts_label(entry)}) — {entry.get('description') or 'no description yet'}")
            lines.append("")

        path = out_dir / f"{card['slug']}.md"
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        written += 1
    info(f"wrote {written} file catalogue(s) to {out_dir}")
    return out_dir


def render_all(config: Config) -> tuple[Path, Path]:
    render_files_md(config)
    return render_assets_md(config), render_attribution_md(config)
