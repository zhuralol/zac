"""Describe what sprites/textures actually depict.

ZAC builds a contact sheet of representative images per pack and asks a vision
model to catalogue it. Two backends are supported:

- `openai`: any OpenAI-compatible /chat/completions endpoint (OpenAI, OpenRouter,
  local vLLM/LM Studio, etc.)
- `ollama`: a local Ollama server with a vision model

When no vision backend is available, `describe --dry-run DIR` writes the contact
sheets and `describe --set <slug>` stores a description written by the agent or
user who looked at them. Descriptions are stored in the card (`content`) so the
whole pipeline stays reproducible and offline-friendly.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cards import save_card
from .config import Config
from .taxonomy import IMAGE_EXTS, ext_of, iter_files
from .util import HttpError, info, http_fetch, now_iso, warn

CELL = 190
LABEL = 20
COLUMNS = 6
MAX_TILES = 24

PROMPT = """\
These are {count} representative images sampled evenly from the itch.io asset pack
"{title}"{author_part}{tags_part}.

Describe what the images actually depict, as if cataloguing game assets for
another developer. Cover:
- art style (pixel art / painted / 3D render) and approximate resolution
- subject matter and recurring motifs
- setting/genre, color palette, perspective (top-down, side-view, isometric)
- whether they are characters, UI elements, effects, props, tiling textures, etc.
- how a game developer would use them

Be concrete and concise (max 120 words). Do not invent file names, licenses, or
content that is not visible. Reply with plain text only.
"""


@dataclass
class VisionSettings:
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None

    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def settings_from_config(config: Config, args: Any = None) -> VisionSettings:
    vision = config.vision
    provider = (getattr(args, "provider", None) or vision.provider or "openai").lower()
    default_base = "http://localhost:11434" if provider == "ollama" else "https://api.openai.com/v1"
    model = getattr(args, "model", None) or vision.model or ("llava" if provider == "ollama" else "gpt-4o-mini")
    base_url = getattr(args, "base_url", None) or vision.base_url or default_base
    key = os.environ.get("ZAC_VISION_API_KEY") or os.environ.get("OPENAI_API_KEY")
    return VisionSettings(provider=provider, model=model, base_url=base_url.rstrip("/"), api_key=key)


# -- sampling / sheets ---------------------------------------------------
def _group_by_dir(files: list[tuple[Path, str]]) -> dict[str, list[tuple[Path, str]]]:
    groups: dict[str, list[tuple[Path, str]]] = {}
    for path, rel in files:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else "."
        groups.setdefault(parent, []).append((path, rel))
    for items in groups.values():
        items.sort(key=lambda item: item[1])
    return groups


def sample_images(card: dict[str, Any], limit: int = MAX_TILES) -> list[tuple[Path, str]]:
    """Pick representative images, spread across subfolders, first frames included."""
    local = card.get("local") or {}
    if not local.get("downloaded") or not local.get("path"):
        return []
    base = Path(local["path"])
    if not base.is_dir():
        return []

    files = [(Path(abs_path), rel) for abs_path, rel in iter_files(str(base)) if ext_of(rel) in IMAGE_EXTS]
    groups = _group_by_dir(files)
    if not groups:
        return []

    total = sum(len(items) for items in groups.values())
    if total <= limit:
        return [item for _, items in sorted(groups.items()) for item in items][:limit]

    per_dir = max(1, limit // len(groups))
    picks: list[tuple[Path, str]] = []
    for _, items in sorted(groups.items()):
        if len(items) <= per_dir:
            picks.extend(items)
            continue
        # Spread across the folder. For animation folders the first frame is
        # often blank, so sample mid-sequence rather than always index 0.
        if per_dir == 1:
            idxs = [len(items) // 2]
        else:
            # Avoid the blank first/last frames: distribute inside the sequence.
            idxs = sorted({
                min(len(items) - 1, max(0, int(round((i + 1) * (len(items) - 1) / (per_dir + 1)))))
                for i in range(per_dir)
            })
        picks.extend(items[i] for i in idxs)

    if len(picks) > limit:
        step = len(picks) / limit
        picks = [picks[min(len(picks) - 1, int(round(i * step)))] for i in range(limit)]
    return picks


def _load(path: Path) -> Any:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        return im.convert("RGBA")


def _font() -> Any:
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, 12)
        except OSError:
            continue
    return ImageFont.load_default()


def build_contact_sheet(card: dict[str, Any], limit: int = MAX_TILES) -> tuple[Any, list[dict[str, Any]]]:
    """Render sampled images into one labeled contact sheet.

    Returns (PIL image, sample metadata). Sample metadata records the relative
    path and cell position so a description can be traced back to real files.
    """
    from PIL import Image, ImageDraw, ImageOps

    samples = sample_images(card, limit)
    if not samples:
        raise ValueError("no images to sample (is the pack downloaded?)")

    rows = math.ceil(len(samples) / COLUMNS)
    sheet = Image.new("RGB", (COLUMNS * CELL, rows * (CELL + LABEL)), (22, 22, 26))
    draw = ImageDraw.Draw(sheet)
    font = _font()
    meta: list[dict[str, Any]] = []

    for index, (path, rel) in enumerate(samples):
        try:
            im = _load(path)
        except Exception as exc:
            warn(f"could not read image {rel}: {exc}")
            continue
        longest = max(im.size)
        resample = Image.NEAREST if longest <= 128 else Image.LANCZOS
        thumb = ImageOps.contain(im, (CELL - 8, CELL - 8), resample)
        cell = Image.new("RGBA", (CELL, CELL), (22, 22, 26, 255))
        cell.alpha_composite(thumb, ((CELL - thumb.width) // 2, (CELL - thumb.height) // 2))
        row, col = divmod(index, COLUMNS)
        x, y = col * CELL, row * (CELL + LABEL)
        sheet.paste(cell.convert("RGB"), (x, y))
        draw.rectangle([x, y + CELL, x + CELL, y + CELL + LABEL], fill=(10, 10, 14))
        draw.text(
            (x + 4, y + CELL + 4),
            f"{index} {Path(rel).name[:26]} ({im.width}x{im.height})",
            fill=(205, 205, 215),
            font=font,
        )
        meta.append(
            {
                "index": index,
                "path": rel,
                "size": [im.width, im.height],
                "cell": [col, row],
            }
        )
    return sheet, meta


def _png_bytes(image: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# -- vision backends -----------------------------------------------------
def build_prompt(card: dict[str, Any], count: int) -> str:
    itch = card.get("itch") or {}
    author = itch.get("author")
    tags = card.get("tags") or []
    return PROMPT.format(
        count=count,
        title=itch.get("title") or card.get("slug"),
        author_part=f" by {author}" if author else "",
        tags_part=f" (tags: {', '.join(tags[:8])})" if tags else "",
    )


def _openai_vision(settings: VisionSettings, prompt: str, png: bytes) -> str:
    if not settings.api_key:
        raise SystemExit(
            "No vision API key found.\n"
            "Set ZAC_VISION_API_KEY (or OPENAI_API_KEY), or use --provider ollama,\n"
            "or run `python -m zac describe --dry-run DIR` and write descriptions with `--set`."
        )
    payload = {
        "model": settings.model,
        "temperature": 0.2,
        "max_tokens": 400,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
                    },
                ],
            }
        ],
    }
    status, _headers, body = http_fetch(
        f"{settings.base_url}/chat/completions",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
    )
    if status != 200:
        raise HttpError(f"{settings.base_url}/chat/completions", status, body)
    data = json.loads(body)
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected vision response: {body[:300]!r}") from exc
    if isinstance(text, list):  # some providers return content parts
        text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
    return str(text).strip()


def _ollama_vision(settings: VisionSettings, prompt: str, png: bytes) -> str:
    payload = {
        "model": settings.model,
        "stream": False,
        "options": {"temperature": 0.2},
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(png).decode()],
            }
        ],
    }
    status, _headers, body = http_fetch(
        f"{settings.base_url}/api/chat",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    if status != 200:
        raise HttpError(f"{settings.base_url}/api/chat", status, body)
    data = json.loads(body)
    try:
        return str(data["message"]["content"]).strip()
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"unexpected ollama response: {body[:300]!r}") from exc


def call_vision(settings: VisionSettings, prompt: str, png: bytes) -> str:
    if settings.provider == "openai":
        return _openai_vision(settings, prompt, png)
    if settings.provider == "ollama":
        return _ollama_vision(settings, prompt, png)
    raise SystemExit(f"unknown vision provider: {settings.provider} (use openai or ollama)")


# -- card integration ----------------------------------------------------
def set_description(config: Config, card: dict[str, Any], text: str, *, model: str = "manual", samples: int | None = None) -> None:
    text = text.strip()
    if not text:
        raise SystemExit("empty description")
    card["content"] = {
        "description": text,
        "model": model,
        "described_at": now_iso(),
        "samples": samples,
    }
    save_card(config, card)


def describe_card(
    config: Config,
    card: dict[str, Any],
    settings: VisionSettings,
    *,
    limit: int = MAX_TILES,
    force: bool = False,
) -> str | None:
    if (card.get("content") or {}).get("description") and not force:
        return None
    sheet, meta = build_contact_sheet(card, limit)
    prompt = build_prompt(card, len(meta))
    text = call_vision(settings, prompt, _png_bytes(sheet))
    if not text:
        raise RuntimeError("vision model returned an empty description")
    set_description(config, card, text, model=settings.label(), samples=len(meta))
    return text


def dry_run(config: Config, cards: list[dict[str, Any]], out_dir: Path, limit: int = MAX_TILES) -> int:
    written = 0
    for card in cards:
        local = card.get("local") or {}
        if not local.get("downloaded"):
            continue
        try:
            sheet, meta = build_contact_sheet(card, limit)
        except ValueError:
            continue
        folder = out_dir / card["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        sheet.save(folder / "sheet.png")
        (folder / "index.json").write_text(
            json.dumps(
                {
                    "slug": card["slug"],
                    "title": (card.get("itch") or {}).get("title"),
                    "samples": meta,
                    "hint": f"python -m zac describe --set {card['slug']} --text-file PATH",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        written += 1
        info(f"  sheet: {folder / 'sheet.png'}")
    return written
