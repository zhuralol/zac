"""Configuration loading (config.toml) with sane project-relative defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import info

DEFAULT_CONFIG = """\
# ZAC — Zhura's Asset Classifier — configuration
#
# List the collections that contain your art assets (including free ones).
# Run `python -m zac collections` to see names and ids.
collections = []

# Which itch.io classifications count as assets. "assets" is the standard value.
classifications = ["assets"]

[paths]
downloads = "downloads"
cards = "assets/cards"
cache = ".cache"
keys = "keys.json"
matches = "assets/matches.json"
assets_md = "ASSETS.md"
attribution_md = "ATTRIBUTION.md"

[vision]
# Provider for `python -m zac describe`: "openai" (any OpenAI-compatible
# /chat/completions endpoint) or "ollama" (local server with a vision model).
# provider = "openai"
# model = "gpt-4o-mini"
# base_url = "https://api.openai.com/v1"
"""


@dataclass
class VisionConfig:
    provider: str = "openai"
    model: str | None = None
    base_url: str | None = None


@dataclass
class Paths:
    downloads: Path
    cards: Path
    cache: Path
    keys: Path
    matches: Path
    assets_md: Path
    attribution_md: Path


@dataclass
class Config:
    root: Path
    collections: list[str] = field(default_factory=list)
    classifications: list[str] = field(default_factory=lambda: ["assets"])
    paths: Paths | None = None
    vision: VisionConfig = field(default_factory=VisionConfig)
    source: Path | None = None

    @property
    def pages_cache(self) -> Path:
        return self.paths.cache / "pages"

    @property
    def scan_cache(self) -> Path:
        return self.paths.cache / "scan"


def _resolve(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


def load_config(config_path: str = "config.toml", *, create: bool = True) -> Config:
    path = Path(config_path).expanduser().resolve()
    root = path.parent
    if not path.exists():
        if not create:
            raise FileNotFoundError(f"config not found: {path}")
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        info(f"created default config: {path}")

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    p = raw.get("paths", {})
    paths = Paths(
        downloads=_resolve(root, p.get("downloads", "downloads")),
        cards=_resolve(root, p.get("cards", "assets/cards")),
        cache=_resolve(root, p.get("cache", ".cache")),
        keys=_resolve(root, p.get("keys", "keys.json")),
        matches=_resolve(root, p.get("matches", "assets/matches.json")),
        assets_md=_resolve(root, p.get("assets_md", "ASSETS.md")),
        attribution_md=_resolve(root, p.get("attribution_md", "ATTRIBUTION.md")),
    )
    collections = [str(c) for c in raw.get("collections", []) if str(c).strip()]
    classifications = [str(c).strip().lower() for c in raw.get("classifications", ["assets"]) if str(c).strip()]
    vision_raw = raw.get("vision", {})
    vision = VisionConfig(
        provider=str(vision_raw.get("provider", "openai")).lower(),
        model=(str(vision_raw["model"]) if vision_raw.get("model") else None),
        base_url=vision_raw.get("base_url"),
    )
    return Config(
        root=root,
        collections=collections,
        classifications=classifications or ["assets"],
        paths=paths,
        vision=vision,
        source=path,
    )
