"""Sprite-sheet grid detection and overlay rendering.

Sheets are detected from alpha gutters: a uniform grid is only considered when
the image has transparency and the cell borders are mostly transparent. This
avoids "detecting" grids inside opaque paintings or textures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

COMMON_SIZES = {8, 12, 16, 24, 32, 40, 48, 64, 96, 128, 160, 192, 256, 320, 384, 512}
MIN_WIDTH = 192
MIN_HEIGHT = 40
MIN_PIXELS = 12000


def _divisors(n: int, lo: int = 8) -> list[int]:
    return [d for d in range(lo, n // 2 + 1) if n % d == 0] + [n]


def _score_grid(alpha: Any, width: int, height: int, cw: int, ch: int) -> tuple[float, float, float] | None:
    cols, rows = width // cw, height // ch
    cells = cols * rows
    if cells < 2 or cells > 600:
        return None
    pix = alpha.load()
    total = hit = 0
    for c in range(1, cols):
        x = c * cw
        for y in range(height):
            total += 1
            if pix[x, y] == 0:
                hit += 1
    for r in range(1, rows):
        y = r * ch
        for x in range(width):
            total += 1
            if pix[x, y] == 0:
                hit += 1
    gutter = hit / total if total else 0.0

    nonempty = 0
    for r in range(rows):
        for c in range(cols):
            found = False
            for y in range(r * ch, (r + 1) * ch, max(1, ch // 16)):
                for x in range(c * cw, (c + 1) * cw, max(1, cw // 16)):
                    if pix[x, y]:
                        found = True
                        break
                if found:
                    break
            if found:
                nonempty += 1
    fill = nonempty / cells
    if fill < 0.55 or gutter < 0.72:
        return None

    score = gutter * 2 + fill * 2
    score += 0.15 * ((cw in COMMON_SIZES) + (ch in COMMON_SIZES))
    if cols > 1 and rows > 1:
        score += 0.15  # prefer 2D layouts over arbitrary strips
    if abs(cw - ch) <= max(2, ch * 0.15) or rows == 1:
        score += 0.2
    return score, gutter, fill


def detect_grid(path: str | Path) -> dict[str, Any] | None:
    """Return {cols, rows, cell_w, cell_h, ...} for a uniform sprite sheet, else None."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.load()
            rgba = im.convert("RGBA")
    except Exception:
        return None
    width, height = rgba.size
    if width < MIN_WIDTH or height < MIN_HEIGHT or width * height < MIN_PIXELS:
        return None
    alpha = rgba.getchannel("A")
    histogram = alpha.histogram()
    transparent = histogram[0] / float(width * height)
    if transparent < 0.02:
        return None

    best: tuple[float, float, float, int, int] | None = None
    for cw in _divisors(width):
        for ch in _divisors(height):
            if cw * ch < 64:
                continue
            scored = _score_grid(alpha, width, height, cw, ch)
            if not scored:
                continue
            score, gutter, fill = scored
            candidate = (score, gutter, fill, cw, ch)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return None
    score, gutter, fill, cw, ch = best
    return {
        "cols": width // cw,
        "rows": height // ch,
        "cell_w": cw,
        "cell_h": ch,
        "gutter": round(gutter, 2),
        "fill": round(fill, 2),
        "cells": (width // cw) * (height // ch),
    }


def annotate_grid(path: str | Path, grid: dict[str, Any], max_side: int = 1400) -> tuple[Any, list[dict[str, Any]]]:
    """Draw cell borders and row,col labels; return (image, cells in source pixels)."""
    from PIL import Image, ImageDraw, ImageFont

    rgba = Image.open(path).convert("RGBA")
    width, height = rgba.size
    scale = min(4.0, max_side / width, max_side / height)
    if scale != 1.0:
        rgba = rgba.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.NEAREST)
    draw = ImageDraw.Draw(rgba)
    font_size = max(9, int(12 * min(scale, 2.0)))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    cw = int(grid["cell_w"] * scale)
    ch = int(grid["cell_h"] * scale)
    cells: list[dict[str, Any]] = []
    for row in range(grid["rows"]):
        for col in range(grid["cols"]):
            x, y = col * cw, row * ch
            draw.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(255, 40, 40, 255), width=2)
            label = f"{row},{col}"
            box = draw.textbbox((0, 0), label, font=font)
            draw.rectangle([x + 1, y + 1, x + box[2] + 7, y + box[3] + 7], fill=(0, 0, 0, 220))
            draw.text((x + 4, y + 3), label, fill=(255, 255, 255, 255), font=font)
            cells.append(
                {
                    "index": row * grid["cols"] + col,
                    "row": row,
                    "col": col,
                    "x": col * grid["cell_w"],
                    "y": row * grid["cell_h"],
                    "w": grid["cell_w"],
                    "h": grid["cell_h"],
                }
            )
    return rgba, cells


def sheet_kind(path: str | Path, grid: dict[str, Any]) -> str:
    """Guess "animation" (frames of one effect) vs "atlas" (distinct items)."""
    from PIL import Image, ImageChops, ImageStat

    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    cw, ch = grid["cell_w"], grid["cell_h"]
    cells = []
    for row in range(grid["rows"]):
        for col in range(grid["cols"]):
            cell = rgba.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
            flat = Image.new("RGB", cell.size, (0, 0, 0))
            flat.paste(cell, mask=cell.getchannel("A"))
            cells.append(flat.resize((24, 24)))
    if len(cells) < 3:
        return "atlas"

    def diff(a, b):
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0

    adjacent = [diff(cells[i], cells[i + 1]) for i in range(len(cells) - 1)]
    # far-apart pairs are a baseline for "different item"
    step = max(2, len(cells) // 4)
    baseline = [diff(cells[i], cells[(i + step) % len(cells)]) for i in range(len(cells))]
    adj = sorted(adjacent)[len(adjacent) // 2]
    base = sorted(baseline)[len(baseline) // 2]
    return "animation" if base > 0 and adj < base * 0.55 else "atlas"
