#!/usr/bin/env python3
"""Search a ZAC asset inventory (assets/cards/*.json) from the command line.

Examples:
    python find_assets.py --tag pixel-art --category images --downloaded
    python find_assets.py "necro sprite"
    python find_assets.py --license "CC BY" --credits
    python find_assets.py --missing --limit 50
    python find_assets.py --json "tileset"

Exits non-zero with a clear message when no inventory is found, so an agent can
tell the user what to do instead of guessing about assets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def find_root(explicit: str | None) -> Path | None:
    """Locate a ZAC checkout (assets/cards). Explicit/env roots are authoritative."""
    explicit_root = explicit or os.environ.get("ZAC_ROOT")
    if explicit_root:
        candidate = Path(explicit_root).expanduser().resolve()
        return candidate if (candidate / "assets" / "cards").is_dir() else None

    candidates: list[Path] = []
    here = Path.cwd()
    candidates.extend([here, *here.parents])
    home = Path.home()
    candidates.extend([home / "Projects" / "zac", home / "Projects" / "art_classifyer"])

    for candidate in candidates:
        candidate = candidate.resolve()
        cards = candidate / "assets" / "cards"
        if cards.is_dir():
            return candidate
    return None


def load_cards(root: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for path in sorted((root / "assets" / "cards").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("slug"):
            cards.append(data)
    return cards


def _haystack(card: dict[str, Any]) -> str:
    itch = card.get("itch") or {}
    parts = [
        card.get("slug", ""),
        itch.get("title", ""),
        itch.get("author", ""),
        card.get("summary", ""),
        card.get("description", ""),
        " ".join(card.get("tags") or []),
        " ".join((card.get("local") or {}).get("notable_files", {}).keys()),
    ]
    return " ".join(str(p) for p in parts).lower()


def _license(card: dict[str, Any]) -> str:
    return str(((card.get("attribution") or {}).get("license") or {}).get("type") or "unspecified")


def _downloaded(card: dict[str, Any]) -> bool:
    return bool((card.get("local") or {}).get("downloaded"))


def _contents(card: dict[str, Any]) -> str:
    local = card.get("local") or {}
    cats = local.get("categories") or {}
    if not cats:
        return "contents unknown (not downloaded)"
    probe = local.get("probe") or {}
    images = probe.get("images") or {}
    audio = probe.get("audio") or {}
    bits: list[str] = []
    for cat, stats in list(cats.items())[:5]:
        label = stats.get("label") or cat
        count = stats.get("count", 0)
        extra = ""
        if cat == "images" and images.get("pixel_art"):
            extra = f", {images['pixel_art']:,} pixel-art"
        if cat == "audio" and (audio.get("sfx") or audio.get("music")):
            extra = f", {audio.get('sfx', 0):,} SFX / {audio.get('music', 0):,} music"
        bits.append(f"{count:,} {label}{extra}")
    return " · ".join(bits)


def matches(card: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.tag:
        card_tags = {_norm(t) for t in (card.get("tags") or [])}
        if not all(_norm(t) in card_tags for t in args.tag):
            return False
    if args.category:
        cats = ((card.get("local") or {}).get("categories") or {})
        if args.category not in cats or not cats[args.category].get("count"):
            return False
    if args.license:
        if args.license.lower() not in _license(card).lower():
            return False
    if args.downloaded and not _downloaded(card):
        return False
    if args.missing and _downloaded(card):
        return False
    if args.query:
        hay = _haystack(card)
        if not all(token in hay for token in args.query.lower().split()):
            return False
    return True


def _score(card: dict[str, Any], query: str | None) -> tuple:
    itch = card.get("itch") or {}
    downloaded = _downloaded(card)
    if not query:
        return (downloaded, (itch.get("title") or "").lower())
    q = query.lower()
    title = (itch.get("title") or "").lower()
    tags = " ".join(card.get("tags") or []).lower()
    return (q in title, q in tags, downloaded, title)


def _print_card(card: dict[str, Any]) -> None:
    itch = card.get("itch") or {}
    local = card.get("local") or {}
    lic = _license(card)
    flag = "⚠ license unspecified" if lic == "unspecified" else lic
    status = "downloaded" if _downloaded(card) else "NOT downloaded"
    print(f"### {itch.get('title') or card.get('slug')} — {itch.get('author') or 'unknown author'}")
    print(f"- Status: {status} · License: {flag} · Acquired: {itch.get('acquired') or 'unknown'}")
    if local.get("path"):
        print(f"- Path: `{local.get('path')}` ({local.get('file_count', 0):,} files, {local.get('total_human') or '?'})")
    if card.get("tags"):
        print(f"- Tags: {', '.join(card['tags'])}")
    print(f"- Contents: {_contents(card)}")
    if not _downloaded(card):
        print("- This pack is metadata-only: contents are unknown until the user downloads it.")
    print(f"- Credit: {(card.get('attribution') or {}).get('credit_line') or 'unknown'}")
    print(f"- Card: assets/cards/{card.get('slug')}.json")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search a ZAC asset inventory (assets/cards/*.json).",
    )
    parser.add_argument("query", nargs="?", default=None, help="free text matched against title, tags, description")
    parser.add_argument("--tag", action="append", default=[], help="require a tag (repeatable, all must match)")
    parser.add_argument("--category", default=None, help="require a category, e.g. images, audio, models_3d, fonts")
    parser.add_argument("--license", default=None, help="substring match on license type (e.g. 'CC BY', 'unspecified')")
    parser.add_argument("--downloaded", action="store_true", help="only assets with local files")
    parser.add_argument("--missing", action="store_true", help="only assets not downloaded yet")
    parser.add_argument("--limit", type=int, default=10, help="max results (default 10)")
    parser.add_argument("--credits", action="store_true", help="print only ready-to-paste credit lines")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    parser.add_argument("--file-query", default=None,
                        help="search per-file descriptions and paths (e.g. 'wooden door')")
    parser.add_argument("--file-limit", type=int, default=50, help="max file matches (default 50)")
    parser.add_argument("--root", default=None, help="ZAC checkout path (default: auto-detect / ZAC_ROOT)")
    args = parser.parse_args(argv)

    root = find_root(args.root)
    if root is None:
        print(
            "No ZAC inventory found.\n"
            "Looked for a checkout containing assets/cards/ (cwd, parents, ~/Projects/zac, "
            "~/Projects/art_classifyer).\n"
            "Pass --root /path/to/ZAC, set ZAC_ROOT, or run `python -m zac ingest` first.",
            file=sys.stderr,
        )
        return 2

    cards = [card for card in load_cards(root) if matches(card, args)]
    if not cards:
        print(f"No matching assets in {root}.", file=sys.stderr)
        print("Try relaxing filters, or check whether the pack still needs to be downloaded/ingested.", file=sys.stderr)
        return 1

    cards.sort(key=lambda c: _score(c, args.query))
    if not args.file_query and args.limit and args.limit > 0:
        cards = cards[: args.limit]

    if args.file_query:
        tokens = [t for t in args.file_query.lower().split() if t]
        hits = []
        for card in cards:
            manifest = (card.get("local") or {}).get("manifest") or []
            for entry in manifest:
                sheet = entry.get("sheet") or {}
                cell_text = " ".join(
                    str(cell.get("description") or "") for cell in sheet.get("cells") or []
                )
                hay = " ".join(
                    [
                        entry.get("path", ""),
                        entry.get("description") or "",
                        entry.get("category", ""),
                        cell_text,
                    ]
                ).lower()
                if all(token in hay for token in tokens):
                    hits.append((card, entry))
        if not hits:
            print(f"No files matching {args.file_query!r}.", file=sys.stderr)
            return 1
        if args.json:
            out = [
                {
                    "pack": (card.get("itch") or {}).get("title") or card.get("slug"),
                    "path": entry.get("path"),
                    "local_path": (card.get("local") or {}).get("path"),
                    "description": entry.get("description"),
                    "category": entry.get("category"),
                    "facts": entry.get("facts"),
                    "sheet": entry.get("sheet"),
                }
                for card, entry in hits[: args.file_limit]
            ]
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        print(f"Found {len(hits)} file(s) matching {args.file_query!r}\n")
        current = None
        for card, entry in hits[: args.file_limit]:
            title = (card.get("itch") or {}).get("title") or card.get("slug")
            if title != current:
                current = title
                print(f"## {title}  ({card.get('slug')})")
            facts = entry.get("facts") or {}
            label = ""
            if facts.get("width") and facts.get("height"):
                label = f" ({facts['width']}x{facts['height']})"
            elif facts.get("duration"):
                label = f" ({facts['duration']:.1f}s)"
            print(f"- `{entry.get('path')}`{label} — {entry.get('description') or 'no description'}")
        return 0

    if args.json:
        out = []
        for card in cards:
            itch = card.get("itch") or {}
            local = card.get("local") or {}
            out.append(
                {
                    "slug": card.get("slug"),
                    "title": itch.get("title"),
                    "author": itch.get("author"),
                    "page": itch.get("url"),
                    "acquired": itch.get("acquired"),
                    "downloaded": _downloaded(card),
                    "path": local.get("path"),
                    "file_count": local.get("file_count"),
                    "tags": card.get("tags") or [],
                    "license": _license(card),
                    "requires_attribution": (card.get("attribution") or {}).get("requires_attribution"),
                    "credit_line": (card.get("attribution") or {}).get("credit_line"),
                    "summary": card.get("summary"),
                    "contents": _contents(card),
                }
            )
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.credits:
        for card in cards:
            line = (card.get("attribution") or {}).get("credit_line")
            if line:
                print(f"- {line}")
        return 0

    print(f"Found {len(cards)} asset(s) in {root}\n")
    for card in cards:
        _print_card(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
