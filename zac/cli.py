"""Command-line interface for ZAC (Zhura's Asset Classifier)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .cards import load_cards, merge_enrich, merge_ingest
from .config import Config, load_config
from .describe import MAX_TILES, describe_card, dry_run, set_description, settings_from_config
from .filedesc import describe_files_for_card
from .importer import import_directory
from .itch_api import ItchAPI, collect_assets, save_download_keys
from .page_parser import POLITE_DELAY, fetch_page
from .render import render_all
from .scanner import run_scan
from .util import HttpError, info, now_iso, slug_from_url, slugify, warn


def _cfg(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def cmd_collections(args: argparse.Namespace) -> int:
    _cfg(args)  # ensure config.toml exists
    api = ItchAPI(base=args.api_base)
    collections = api.collections()
    if not collections:
        print("No collections found on this account.")
        return 0
    print(f"{'ID':>10}  {'NAME':<40} {'GAMES':>6}  PRIVATE")
    for coll in collections:
        print(
            f"{str(coll.get('id')):>10}  {str(coll.get('title'))[:40]:<40} "
            f"{coll.get('games_count', '?'):>6}  {'yes' if coll.get('private') else 'no'}"
        )
    print("\nAdd the collection names (or ids) you want to ingest to config.toml:")
    print('  collections = ["My Game Assets"]')
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    api = ItchAPI(base=args.api_base)
    records, rejected = collect_assets(cfg, api)
    changed = save_download_keys(cfg, records)
    stats = merge_ingest(cfg, records)

    print(f"assets found: {len(records)}")
    if rejected:
        print("excluded by classification:")
        for cls, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
            print(f"  {cls or 'unknown'}: {count}")
    if changed:
        info(f"stored {changed} download key(s) in {cfg.paths.keys} (gitignored)")
    print(f"cards: {stats['created']} created, {stats['refreshed']} refreshed")
    return 0


def cmd_ingest_url(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    url = args.url.strip()
    parsed = fetch_page(url, cfg.pages_cache, force=True)
    if not parsed:
        warn(f"could not fetch {url}")
        return 1
    slug = slug_from_url(url) or slugify(parsed.get("title") or "asset")
    record = {
        "game_id": f"manual-{slug}",
        "title": parsed.get("title") or slug,
        "url": url,
        "author": parsed.get("author"),
        "author_url": parsed.get("author_url"),
        "classification": args.classification,
        "cover_url": parsed.get("cover_url"),
        "published_at": None,
        "created_at": None,
        "min_price": None,
        "traits": [],
        "acquired": "manual",
        "owned_key": False,
        "sources": [f"manual:{url}"],
        "download_key": None,
    }
    merge_ingest(cfg, {str(record["game_id"]): record})
    merge_enrich(cfg, slug, parsed)
    print(f"added manual card: {slug}")
    return 0


def cmd_import_dir(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    downloads = Path(args.downloads_dir).expanduser().resolve() if args.downloads_dir else None
    stats = import_directory(cfg, downloads, include_empty=args.include_empty)
    print(f"cards created: {stats['created']}, already present: {stats['skipped']}, empty skipped: {stats['empty']}")
    print("next: run `python -m zac scan` (with the same --downloads-dir) and then `python -m zac render`")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    assert cfg.paths is not None
    cards = sorted(
        load_cards(cfg.paths.cards).values(),
        key=lambda c: ((c.get("itch") or {}).get("title") or c.get("slug") or "").lower(),
    )
    if not cards:
        warn(f"no cards in {cfg.paths.cards}; run `python -m zac ingest` or `import-dir` first")
        return 1

    if args.set:
        card = next((c for c in cards if c.get("slug") == args.set), None)
        if card is None:
            warn(f"no card with slug {args.set!r}")
            return 1
        if args.text_file:
            text = Path(args.text_file).expanduser().read_text(encoding="utf-8")
        elif args.text:
            text = args.text
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            warn("provide --text, --text-file, or pipe the description on stdin")
            return 1
        set_description(cfg, card, text)
        print(f"stored description for {card['slug']}")
        return 0

    if args.slug:
        wanted = set(args.slug)
        cards = [c for c in cards if c.get("slug") in wanted]
    cards = [c for c in cards if (c.get("local") or {}).get("downloaded")]
    if args.limit:
        cards = cards[: args.limit]
    if not cards:
        print("no downloaded asset packs to describe")
        return 0

    if args.dry_run:
        out_dir = Path(args.dry_run).expanduser().resolve()
        written = dry_run(cfg, cards, out_dir, args.tiles)
        print(f"wrote {written} contact sheet(s) to {out_dir}")
        print("inspect them, then store a description with:")
        print("  python -m zac describe --set <slug> --text-file <file>")
        return 0

    settings = settings_from_config(cfg, args)
    done = skipped = failed = 0
    for index, card in enumerate(cards, 1):
        slug = card["slug"]
        if (card.get("content") or {}).get("description") and not args.force:
            skipped += 1
            continue
        info(f"[{index}/{len(cards)}] {slug} via {settings.label()}")
        try:
            text = describe_card(cfg, card, settings, limit=args.tiles, force=args.force)
        except (HttpError, RuntimeError, OSError) as exc:
            failed += 1
            warn(f"  failed: {exc}")
            continue
        except SystemExit as exc:
            print(exc.code, file=sys.stderr)
            return 1
        if text:
            done += 1
        else:
            skipped += 1
    print(f"described {done}, skipped {skipped}, failed {failed}")
    if failed:
        print("tip: without a vision backend use --dry-run DIR plus --set, or configure [vision] in config.toml")
    return 0 if not failed else 1


def cmd_describe_files(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    assert cfg.paths is not None
    cards = sorted(
        load_cards(cfg.paths.cards).values(),
        key=lambda c: ((c.get("itch") or {}).get("title") or c.get("slug") or "").lower(),
    )
    cards = [c for c in cards if (c.get("local") or {}).get("downloaded")]
    if args.slug:
        wanted = set(args.slug)
        cards = [c for c in cards if c.get("slug") in wanted]
    if args.limit:
        cards = cards[: args.limit]
    if not cards:
        print("no downloaded asset packs to catalogue")
        return 0

    settings = settings_from_config(cfg, args)
    totals = {"described": 0, "sheets": 0, "batches": 0, "failed": 0}
    for index, card in enumerate(cards, 1):
        info(f"[{index}/{len(cards)}] {card['slug']} via {settings.label()}")
        try:
            stats = describe_files_for_card(
                cfg,
                card,
                settings,
                force=args.force,
                batch_tiles=args.batch,
                include_sheets=not args.no_sheets,
            )
        except SystemExit as exc:
            print(exc.code, file=sys.stderr)
            return 1
        except Exception as exc:
            warn(f"  failed: {exc}")
            continue
        for key in totals:
            totals[key] += stats.get(key, 0)
    print(
        f"files described: {totals['described']:,}; sheets: {totals['sheets']}; "
        f"batches: {totals['batches']}"
    )
    print("run `python -m zac render` to refresh ASSETS.md and assets/files/*.md")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    assert cfg.paths is not None
    cards = load_cards(cfg.paths.cards)
    if not cards:
        warn(f"no cards in {cfg.paths.cards}; run `python -m zac ingest` first")
        return 1
    targets = sorted(cards.values(), key=lambda c: ((c.get("itch") or {}).get("title") or ""))
    if args.limit:
        targets = targets[: args.limit]
    total = len(targets)
    enriched = 0
    for idx, card in enumerate(targets, 1):
        url = (card.get("itch") or {}).get("url")
        if not url:
            continue
        info(f"[{idx}/{total}] {card.get('slug')}")
        parsed = fetch_page(url, cfg.pages_cache, force=args.force)
        if parsed:
            merge_enrich(cfg, card["slug"], parsed)
            enriched += 1
        if idx < total:
            time.sleep(args.delay)
    print(f"enriched {enriched}/{total} cards")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.downloads_dir:
        assert cfg.paths is not None
        cfg.paths.downloads = Path(args.downloads_dir).expanduser().resolve()
    stats = run_scan(cfg, deep=not args.no_probe, force=args.force, interactive=not args.no_interactive)
    print(f"folders matched: {stats['matched']}, scanned: {stats['scanned']}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    assets_md, attribution_md = render_all(cfg)
    print(f"rendered {assets_md}")
    print(f"rendered {attribution_md}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    _cfg(args)  # ensure config.toml exists before any step
    print("== ingest ==", file=sys.stderr)
    if ItchAPI(base=args.api_base).key:
        rc = cmd_ingest(args)
        if rc:
            return rc
    else:
        warn("ITCH_API_KEY not set: skipping ingest (collections/owned inventory unchanged)")
    print("== enrich ==", file=sys.stderr)
    cmd_enrich(args)
    print("== scan ==", file=sys.stderr)
    cmd_scan(args)
    print("== render ==", file=sys.stderr)
    cmd_render(args)
    print(f"update complete at {now_iso()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zac",
        description="Inventory owned itch.io art assets as JSON cards + LLM-friendly Markdown.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="config.toml", help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--api-base", default="https://api.itch.io", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collections", help="list your itch.io collections (to fill config.toml)")
    p.set_defaults(func=cmd_collections)

    p = sub.add_parser("ingest", help="pull owned keys + configured collections into cards")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ingest-url", help="add a single asset page manually (delisted/private assets)")
    p.add_argument("url")
    p.add_argument("--classification", default="assets")
    p.set_defaults(func=cmd_ingest_url)

    p = sub.add_parser("import-dir", help="bootstrap cards from a downloads folder (no API key needed)")
    p.add_argument("--downloads-dir", default=None, help="folder containing one subfolder per asset")
    p.add_argument("--include-empty", action="store_true", help="also create cards for empty folders")
    p.set_defaults(func=cmd_import_dir)

    p = sub.add_parser("describe", help="describe what sprites/textures show using a vision model")
    p.add_argument("--slug", action="append", default=[], help="only these card slugs (repeatable)")
    p.add_argument("--limit", type=int, default=0, help="only the first N packs")
    p.add_argument("--tiles", type=int, default=MAX_TILES, help=f"images per contact sheet (default {MAX_TILES})")
    p.add_argument("--provider", choices=["openai", "ollama"], default=None)
    p.add_argument("--model", default=None, help="vision model name")
    p.add_argument("--base-url", default=None, help="API base URL (OpenAI-compatible) or Ollama host")
    p.add_argument("--force", action="store_true", help="re-describe packs that already have a description")
    p.add_argument("--dry-run", metavar="DIR", default=None,
                   help="write contact sheets for human/agent review instead of calling a model")
    p.add_argument("--set", metavar="SLUG", default=None, help="store a description you wrote after looking at a sheet")
    p.add_argument("--text", default=None, help="description text for --set")
    p.add_argument("--text-file", default=None, help="read description text for --set from a file")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("describe-files", help="catalogue every file, including sprite-sheet grids and cells")
    p.add_argument("--slug", action="append", default=[], help="only these card slugs (repeatable)")
    p.add_argument("--limit", type=int, default=0, help="only the first N packs")
    p.add_argument("--batch", type=int, default=MAX_TILES, help=f"tiles per request (default {MAX_TILES})")
    p.add_argument("--no-sheets", action="store_true", help="skip sprite-sheet grid overlays")
    p.add_argument("--force", action="store_true", help="re-describe files that already have a description")
    p.add_argument("--provider", choices=["openai", "ollama"], default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None)
    p.set_defaults(func=cmd_describe_files)

    p = sub.add_parser("enrich", help="scrape asset pages for description, tags, author")
    p.add_argument("--force", action="store_true", help="ignore page cache")
    p.add_argument("--limit", type=int, default=0, help="only process the first N cards")
    p.add_argument("--delay", type=float, default=POLITE_DELAY, help="seconds between requests")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("scan", help="scan downloads/ and analyze file contents")
    p.add_argument("--downloads-dir", default=None, help="override downloads path")
    p.add_argument("--no-probe", action="store_true", help="skip deep probing (fast)")
    p.add_argument("--force", action="store_true", help="ignore probe cache")
    p.add_argument("--no-interactive", action="store_true", help="never prompt for ambiguous matches")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("render", help="regenerate ASSETS.md and ATTRIBUTION.md")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("update", help="ingest + enrich + scan + render")
    p.add_argument("--force", action="store_true", help="ignore page and probe caches")
    p.add_argument("--no-probe", action="store_true")
    p.add_argument("--no-interactive", action="store_true")
    p.add_argument("--downloads-dir", default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--delay", type=float, default=POLITE_DELAY)
    p.set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
