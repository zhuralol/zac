"""Per-file asset catalogue: descriptions for every file, including sprite sheets.

Three strategies keep this affordable for thousands of files:

- sequences (numbered frames in one folder) are described once and every frame
  gets "<description> (frame i/N)"
- standalone images are described in batches of `batch_tiles` via a labeled
  contact sheet, with the model returning a JSON map
- sprite sheets get a grid overlay and per-cell coordinates; atlases get a
  description per cell, animation strips get frame numbers

Non-image files (audio, 3D, fonts, docs) get metadata-based descriptions without
a model call.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cards import save_card
from .config import Config
from .describe import VisionSettings, call_vision, png_bytes
from .sheets import annotate_grid, detect_grid
from .util import info, warn

CELL = 190
LABEL = 20
COLUMNS = 6
MAX_TILES = 24
SEQUENCE_MIN = 8

_SEQ = re.compile(r"^(?P<prefix>.+?)[ _-]?(?P<num>\d{1,4})$")
_HUMANIZE = re.compile(r"[_-]+")

SHEET_PROMPT = """\
This is one sprite sheet from the itch.io asset pack "{title}". A red overlay
labels every cell with "row,col". The grid is {cols} columns by {rows} rows,
each cell {cw}x{ch} pixels.

Return a JSON object exactly like:
{{"kind": "atlas" or "animation",
  "description": "one sentence, max 30 words",
  "cells": {{"0,0": "max 12 words", "0,1": "..."}}}}

Use "animation" when the cells are frames of a single animation (VFX, character
motion); in that case "cells" may be {{}}. Use "atlas" when cells are distinct
items; then describe every non-empty cell. Do not invent content that is not
visible. JSON only, no markdown.
"""

ANIM_SHEETS_PROMPT = """\
These tiles are sprite sheets of animations from the itch.io asset pack "{title}".
Each tile is labeled "#n". Describe what effect or animation each sheet shows
(max 15 words) and how a game developer would use it. JSON only, mapping each n:
{{"0": "Red fireball burst, 8 frames; spell impact.", "1": "..."}}
"""

BATCH_PROMPT = """\
These tiles come from the itch.io asset pack "{title}". Each tile is labeled
"#n". Tiles sharing the same n are different frames or views of the same asset.

Return a JSON object mapping every n to a concise description (max 25 words) of
what that asset shows and how a game developer would use it. Example:
{{"0": "Seamless mossy stone wall texture; dungeon levels.", "1": "..."}}
Do not describe the tile layout or filenames. JSON only, no markdown.
"""


@dataclass
class Item:
    kind: str  # "single" | "sequence" | "sheet"
    files: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    grid: dict[str, Any] | None = None
    key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _base(card: dict[str, Any]) -> Path | None:
    local = card.get("local") or {}
    if not local.get("downloaded") or not local.get("path"):
        return None
    base = Path(local["path"])
    return base if base.is_dir() else None


def _sample_positions(count: int, take: int) -> list[int]:
    if count <= take:
        return list(range(count))
    if take == 1:
        return [count // 2]
    return sorted({int(round((i + 1) * (count - 1) / (take + 1))) for i in range(take)})


SHEET_NAME = re.compile(r"sheet|atlas", re.IGNORECASE)


def _pack_hint(card: dict[str, Any]) -> str:
    itch = card.get("itch") or {}
    text = " ".join(
        [str(itch.get("title") or ""), str(card.get("description") or ""), " ".join(card.get("tags") or [])]
    ).lower()
    if re.search(r"\b(effect|effects|vfx|spell|explosion|particle|magic|impact)\b", text):
        return "effects"
    if re.search(r"\b(character|characters|avatar|portrait|faceset)\b", text):
        return "characters"
    if re.search(r"\b(ui|interface|menu|hud)\b", text):
        return "ui"
    return ""


def _cells_for_grid(grid: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "index": row * grid["cols"] + col,
            "row": row,
            "col": col,
            "x": col * grid["cell_w"],
            "y": row * grid["cell_h"],
            "w": grid["cell_w"],
            "h": grid["cell_h"],
        }
        for row in range(grid["rows"])
        for col in range(grid["cols"])
    ]


def build_items(card: dict[str, Any], *, include_sheets: bool = True) -> list[Item]:
    """Group image files into singles, sequences and sprite sheets."""
    base = _base(card)
    hint = _pack_hint(card)
    if base is None:
        return []
    manifest = card.get("local", {}).get("manifest") or []
    images = [
        f for f in manifest
        if f.get("category") == "images" and (f.get("facts") or {}).get("width")
    ]

    items: list[Item] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for entry in images:
        facts = entry.get("facts") or {}
        width, height = int(facts.get("width") or 0), int(facts.get("height") or 0)
        named_sheet = bool(SHEET_NAME.search(entry["path"]))
        if include_sheets and width >= 192 and height >= 40 and width * height >= 12000:
            if hint == "characters" and not named_sheet:
                grid = None
            else:
                grid = (entry.get("sheet") or {}).get("grid") or detect_grid(base / entry["path"])
            confident = bool(grid and grid.get("cells", 0) >= 4
                             and grid.get("gutter", 0) >= 0.85 and grid.get("fill", 0) >= 0.85
                             and min(grid.get("cell_w", 0), grid.get("cell_h", 0)) >= 16)
            if grid and (named_sheet or confident):
                # Only effect packs are trusted to be animation strips without a
                # model call; everything else is verified per-sheet as an atlas.
                kind = "animation" if hint == "effects" else "atlas"
                entry.setdefault("sheet", {})["grid"] = grid
                entry["sheet"]["kind"] = kind
                entry["sheet"]["cells"] = _cells_for_grid(grid)
                items.append(Item(kind="sheet", files=[entry], samples=[entry],
                                  grid=grid, key=entry["path"]))
                continue
        stem = Path(entry["path"]).stem
        match = _SEQ.match(stem)
        parent = entry["path"].rsplit("/", 1)[0] if "/" in entry["path"] else "."
        if match and match.group("prefix").strip("_- "):
            groups.setdefault((parent, match.group("prefix").lower()), []).append(entry)
        else:
            items.append(Item(kind="single", files=[entry], samples=[entry], key=entry["path"]))

    for (parent, prefix), files in groups.items():
        files.sort(key=lambda f: f["path"])
        if len(files) >= SEQUENCE_MIN:
            positions = _sample_positions(len(files), 3)
            items.append(
                Item(
                    kind="sequence",
                    files=files,
                    samples=[files[i] for i in positions],
                    key=f"{parent}/{prefix}",
                )
            )
        else:
            for entry in files:
                items.append(Item(kind="single", files=[entry], samples=[entry], key=entry["path"]))

    items.sort(key=lambda item: item.files[0]["path"])
    return items


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _load_rgba(path: Path) -> Any:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        return im.convert("RGBA")


def _tile_sheet(items: list[Item], base: Path) -> tuple[Any, int]:
    """Render labeled tiles for a batch of items. Labels are "#<item index>"."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    layout: list[tuple[int, Path]] = []
    for index, item in enumerate(items):
        for entry in item.samples:
            layout.append((index, base / entry["path"]))
            if len(layout) >= MAX_TILES:
                break
        if len(layout) >= MAX_TILES:
            break
    rows = max(1, math.ceil(len(layout) / COLUMNS))
    sheet = Image.new("RGB", (COLUMNS * CELL, rows * (CELL + LABEL)), (22, 22, 26))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except OSError:
        font = ImageFont.load_default()

    for position, (item_index, path) in enumerate(layout):
        try:
            im = _load_rgba(path)
        except Exception:
            continue
        longest = max(im.size)
        resample = Image.NEAREST if longest <= 128 else Image.LANCZOS
        thumb = ImageOps.contain(im, (CELL - 8, CELL - 8), resample)
        cell = Image.new("RGBA", (CELL, CELL), (22, 22, 26, 255))
        cell.alpha_composite(thumb, ((CELL - thumb.width) // 2, (CELL - thumb.height) // 2))
        row, col = divmod(position, COLUMNS)
        x, y = col * CELL, row * (CELL + LABEL)
        sheet.paste(cell.convert("RGB"), (x, y))
        draw.rectangle([x, y + CELL, x + CELL, y + CELL + LABEL], fill=(10, 10, 14))
        draw.text((x + 4, y + CELL + 4), f"#{item_index}", fill=(235, 235, 245), font=font)
    return sheet, len(layout)


def _apply_item(item: Item, description: str) -> None:
    description = description.strip()
    if not description:
        return
    if item.kind == "sequence":
        total = len(item.files)
        for index, entry in enumerate(item.files):
            entry["description"] = f"{description} (frame {index + 1}/{total})"
    else:
        for entry in item.files:
            entry["description"] = description


def _apply_sheet(entry: dict[str, Any], grid: dict[str, Any], data: dict[str, Any], fallback_kind: str) -> None:
    kind = str(data.get("kind") or fallback_kind).lower()
    if kind not in ("atlas", "animation"):
        kind = fallback_kind
    description = str(data.get("description") or "").strip()
    if description:
        entry["description"] = description
    cells_data = data.get("cells") if isinstance(data.get("cells"), dict) else {}

    sheet = entry.setdefault("sheet", {})
    sheet["grid"] = grid
    sheet["kind"] = kind
    cells = []
    for cell in sheet.get("cells") or []:
        cell = dict(cell)
        if kind == "animation":
            cell["description"] = f"frame {cell['index'] + 1}/{grid['cells']}"
        else:
            value = cells_data.get(f"{cell['row']},{cell['col']}") or cells_data.get(str(cell["index"]))
            cell["description"] = str(value).strip() if value else None
        cells.append(cell)
    if not cells:
        # build coordinates if the overlay metadata was lost
        cells = [
            {
                "index": row * grid["cols"] + col,
                "row": row,
                "col": col,
                "x": col * grid["cell_w"],
                "y": row * grid["cell_h"],
                "w": grid["cell_w"],
                "h": grid["cell_h"],
                "description": (
                    f"frame {row * grid['cols'] + col + 1}/{grid['cells']}"
                    if kind == "animation"
                    else None
                ),
            }
            for row in range(grid["rows"])
            for col in range(grid["cols"])
        ]
    sheet["cells"] = cells


def _humanize(stem: str) -> str:
    text = _HUMANIZE.sub(" ", stem).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:1].upper() + text[1:] if text else stem


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = float(seconds)
    if seconds < 3:
        return f"{seconds:.2f}s"
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def fallback_description(entry: dict[str, Any]) -> str | None:
    """Metadata-only description for non-image files."""
    facts = entry.get("facts") or {}
    category = entry.get("category")
    name = _humanize(Path(entry["path"]).stem)
    if category == "audio":
        role = facts.get("role")
        length = _duration(facts.get("duration"))
        if role == "sfx":
            return f"Sound effect{f' ({length})' if length else ''}: {name}."
        if role == "music":
            return f"Music track{f' ({length})' if length else ''}: {name}."
        return f"Audio clip{f' ({length})' if length else ''}: {name}."
    if category == "models_3d":
        bits = []
        if facts.get("meshes"):
            bits.append(f"{facts['meshes']} meshes")
        if facts.get("vertices"):
            bits.append(f"{facts['vertices']} vertices")
        suffix = f" ({', '.join(bits)})" if bits else ""
        return f"3D model{suffix}: {name}."
    if category == "fonts":
        glyphs = f", {facts['glyphs']} glyphs" if facts.get("glyphs") else ""
        return f"Font{glyphs}: {name}."
    if category == "vector":
        return f"Vector graphic: {name}."
    if category == "shaders":
        return f"Shader source: {name}."
    if category == "source_projects":
        return f"Editable source project: {name}."
    if category == "tilemaps":
        return f"Tilemap or atlas metadata: {name}."
    if category == "docs":
        return f"Document: {name}."
    if category == "archives":
        return f"Archive container: {name} (contents are listed separately)."
    return f"File: {name}."


def fill_nonimage_descriptions(card: dict[str, Any]) -> int:
    count = 0
    for entry in card.get("local", {}).get("manifest") or []:
        if entry.get("category") == "images" or entry.get("description"):
            continue
        value = fallback_description(entry)
        if value:
            entry["description"] = value
            count += 1
    return count


ANIM_CELL = 300
ANIM_COLS = 4
ANIM_LABEL = 20


def _anim_sheet_thumbnails(items: list[Item], base: Path) -> tuple[Any, int]:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    rows = max(1, math.ceil(len(items) / ANIM_COLS))
    sheet = Image.new("RGB", (ANIM_COLS * ANIM_CELL, rows * (ANIM_CELL + ANIM_LABEL)), (22, 22, 26))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    used = 0
    for index, item in enumerate(items):
        try:
            im = _load_rgba(base / item.files[0]["path"])
        except Exception:
            continue
        thumb = ImageOps.contain(im, (ANIM_CELL - 8, ANIM_CELL - 8), Image.LANCZOS)
        cell = Image.new("RGBA", (ANIM_CELL, ANIM_CELL), (22, 22, 26, 255))
        cell.alpha_composite(thumb, ((ANIM_CELL - thumb.width) // 2, (ANIM_CELL - thumb.height) // 2))
        row, col = divmod(index, ANIM_COLS)
        x, y = col * ANIM_CELL, row * (ANIM_CELL + ANIM_LABEL)
        sheet.paste(cell.convert("RGB"), (x, y))
        draw.rectangle([x, y + ANIM_CELL, x + ANIM_CELL, y + ANIM_CELL + ANIM_LABEL], fill=(10, 10, 14))
        draw.text((x + 4, y + ANIM_CELL + 3), f"#{index}", fill=(235, 235, 245), font=font)
        used += 1
    return sheet, used


def describe_files_for_card(
    config: Config,
    card: dict[str, Any],
    settings: VisionSettings,
    *,
    force: bool = False,
    batch_tiles: int = MAX_TILES,
    include_sheets: bool = True,
) -> dict[str, int]:
    base = _base(card)
    if base is None:
        return {"described": 0, "sheets": 0, "batches": 0, "failed": 0}
    nonimage = fill_nonimage_descriptions(card)

    items = build_items(card, include_sheets=include_sheets)
    pending = [
        item for item in items
        if force or any(not entry.get("description") for entry in item.files)
    ]
    sheets = [item for item in pending if item.kind == "sheet"]
    anim_sheets = [i for i in sheets if (i.files[0].get("sheet") or {}).get("kind") == "animation"]
    atlas_sheets = [i for i in sheets if i not in anim_sheets]
    others = [item for item in pending if item.kind != "sheet"]

    described = 0
    for index, item in enumerate(atlas_sheets, 1):
        entry = item.files[0]
        info(f"    atlas sheet {index}/{len(atlas_sheets)}: {entry['path']}")
        try:
            image, cells = annotate_grid(base / entry["path"], item.grid or {})
            prompt = SHEET_PROMPT.format(
                title=(card.get("itch") or {}).get("title") or card.get("slug"),
                cols=item.grid["cols"],
                rows=item.grid["rows"],
                cw=item.grid["cell_w"],
                ch=item.grid["cell_h"],
            )
            text = call_vision(settings, prompt, png_bytes(image))
            data = _parse_json(text)
            if not data:
                raise RuntimeError(f"model did not return JSON: {text[:120]!r}")
            entry.setdefault("sheet", {})["cells"] = cells
            _apply_sheet(entry, item.grid or {}, data, (entry.get("sheet") or {}).get("kind") or "atlas")
            described += 1
        except Exception as exc:
            warn(f"      sheet failed: {exc}")
        save_card(config, card)

    # Animation sheets: batch thumbnails, one description each, frame coords kept.
    ANIM_BATCH = 12
    for start in range(0, len(anim_sheets), ANIM_BATCH):
        batch = anim_sheets[start:start + ANIM_BATCH]
        image, tiles = _anim_sheet_thumbnails(batch, base)
        if not tiles:
            continue
        info(f"    animation sheets {start + 1}-{start + len(batch)}/{len(anim_sheets)}")
        try:
            prompt = ANIM_SHEETS_PROMPT.format(
                title=(card.get("itch") or {}).get("title") or card.get("slug")
            )
            text = call_vision(settings, prompt, png_bytes(image))
            data = _parse_json(text) or {}
            for item_index, item in enumerate(batch):
                entry = item.files[0]
                description = data.get(str(item_index))
                grid = item.grid or {}
                sheet = entry.setdefault("sheet", {})
                sheet["grid"] = grid
                sheet["kind"] = "animation"
                cells = _cells_for_grid(grid)
                for cell in cells:
                    cell["description"] = f"frame {cell['index'] + 1}/{grid.get('cells', len(cells))}"
                sheet["cells"] = cells
                if description:
                    entry["description"] = str(description).strip()
                    described += 1
        except Exception as exc:
            warn(f"      animation sheets failed: {exc}")
        save_card(config, card)

    batches = 0
    batch_list: list[list[Item]] = []
    current: list[Item] = []
    tiles_used = 0
    for item in others:
        need = max(1, len(item.samples))
        if current and tiles_used + need > batch_tiles:
            batch_list.append(current)
            current, tiles_used = [], 0
        current.append(item)
        tiles_used += need
    if current:
        batch_list.append(current)

    for batch in batch_list:
        batches += 1
        image, tiles = _tile_sheet(batch, base)
        if not tiles:
            continue
        info(f"    batch {batches}: {len(batch)} items / {tiles} tiles")
        try:
            prompt = BATCH_PROMPT.format(title=(card.get("itch") or {}).get("title") or card.get("slug"))
            text = call_vision(settings, prompt, png_bytes(image))
            data = _parse_json(text) or {}
            for item_index, item in enumerate(batch):
                description = data.get(str(item_index))
                if description:
                    _apply_item(item, str(description))
                    described += len(item.files)
        except Exception as exc:
            warn(f"      batch failed: {exc}")
        save_card(config, card)

    if nonimage:
        save_card(config, card)
    return {"described": described, "sheets": len(sheets), "batches": batches, "failed": 0}
