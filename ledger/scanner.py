"""Scan downloaded asset folders: match them to cards, walk files, probe, aggregate."""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .probe import MAX_PROBE_BYTES, probe_bytes, probe_path
from .taxonomy import (
    category_label,
    classify,
    is_archive,
    is_junk_dir,
    is_junk_file,
    is_probable_license_doc,
)
from .util import human_bytes, info, now_iso, read_json, sha1, warn, write_json

_SEQUENCE = re.compile(r"^(?P<prefix>.*?)[ _-]?(?P<num>\d{1,3})$", re.IGNORECASE)
_MAX_MANIFEST = 2000
_MAX_NOTABLE = 8
_MAX_DOCS = 6
_MAX_DOC_BYTES = 2 * 1024 * 1024
_MAX_DOC_PROBE = 5 * 1024 * 1024

_PROBE_CATEGORIES = {"images", "audio", "models_3d", "fonts", "docs", "shaders"}


def _candidates(card: dict[str, Any]) -> list[str]:
    from .util import normalize_name, slug_from_url

    values = [card.get("slug", ""), (card.get("itch") or {}).get("title", "")]
    url_slug = slug_from_url((card.get("itch") or {}).get("url", ""))
    if url_slug:
        values.append(url_slug)
    return [v for v in (normalize_name(x) for x in values) if v]


def match_score(folder_name: str, card: dict[str, Any]) -> float:
    import difflib

    from .util import normalize_name

    target = normalize_name(folder_name)
    if not target:
        return 0.0
    best = 0.0
    for cand in _candidates(card):
        if target == cand:
            return 1.0
        if target.startswith(cand) or cand.startswith(target):
            best = max(best, 0.93)
        best = max(best, difflib.SequenceMatcher(None, target, cand).ratio())
    return best


def match_folders(
    config: Config, cards: dict[str, dict[str, Any]], *, interactive: bool = True
) -> dict[str, str]:
    """Map downloads/<folder> -> card slug, persisting decisions in matches.json."""
    if not config.paths:
        return {}
    downloads = config.paths.downloads
    if not downloads.exists():
        return {}
    stored = read_json(config.paths.matches, default={}) or {}
    folder_map: dict[str, str] = dict(stored.get("folders") or {})
    ambiguous: list[tuple[str, list[tuple[float, str]]]] = []

    folders = sorted(p.name for p in downloads.iterdir() if p.is_dir() and not is_junk_dir(p.name))
    for folder in folders:
        if folder in folder_map and folder_map[folder] in cards:
            continue
        scored = sorted(((match_score(folder, c), slug) for slug, c in cards.items()), reverse=True)
        scored = [(s, slug) for s, slug in scored if s >= 0.72]
        if not scored:
            info(f"  no matching asset card for folder: {folder}")
            folder_map.pop(folder, None)
            continue
        top_score, top_slug = scored[0]
        close = [x for x in scored[1:3] if top_score - x[0] < 0.05]
        if top_score >= 0.90 and not close:
            folder_map[folder] = top_slug
            continue
        ambiguous.append((folder, scored[:5]))

    if ambiguous and interactive and sys.stdin.isatty():
        for folder, options in ambiguous:
            print(f"\nWhich asset is the folder {folder!r}?")
            for idx, (score, slug) in enumerate(options, 1):
                print(f"  {idx}. {slug}  (match {score:.2f})")
            print("  s. skip this folder")
            while True:
                answer = input("> ").strip().lower()
                if answer in ("s", "skip", ""):
                    folder_map.pop(folder, None)
                    break
                if answer.isdigit() and 1 <= int(answer) <= len(options):
                    folder_map[folder] = options[int(answer) - 1][1]
                    break
    elif ambiguous:
        for folder, options in ambiguous:
            score, slug = options[0]
            info(f"  assuming {folder!r} -> {slug} (match {score:.2f}); edit {config.paths.matches.name} to change")
            folder_map[folder] = slug

    write_json(
        config.paths.matches,
        {"folders": folder_map, "updated_at": now_iso()},
    )
    return folder_map


def _new_aggregate() -> dict[str, Any]:
    return {
        "categories": defaultdict(lambda: {"count": 0, "bytes": 0}),
        "manifest": [],
        "manifest_truncated": False,
        "notable": defaultdict(list),
        "docs": [],
        "doc_texts": [],
        "probe": {
            "images": {
                "count": 0,
                "pixel_art": 0,
                "size_buckets": defaultdict(int),
                "palette_buckets": defaultdict(int),
                "animated_frames": 0,
            },
            "audio": {"count": 0, "sfx": 0, "music": 0, "other": 0, "total_duration": 0.0},
            "models_3d": {"count": 0, "meshes": 0, "animations": 0},
            "unprobed": 0,
        },
        "file_count": 0,
        "total_bytes": 0,
        "sequences": defaultdict(set),
        "archive_count": 0,
    }


def _add_file(agg: dict[str, Any], rel: str, size: int, *, notable: bool = True) -> str:
    cat = classify(rel)
    agg["categories"][cat]["count"] += 1
    agg["categories"][cat]["bytes"] += size
    agg["file_count"] += 1
    agg["total_bytes"] += size
    if len(agg["manifest"]) < _MAX_MANIFEST:
        agg["manifest"].append({"path": rel, "size": size, "category": cat})
    else:
        agg["manifest_truncated"] = True
    if notable and len(agg["notable"][cat]) < _MAX_NOTABLE:
        agg["notable"][cat].append(rel)
    return cat


def _record_facts(agg: dict[str, Any], rel: str, facts: dict[str, Any]) -> None:
    kind = facts.get("kind")
    probe = agg["probe"]
    if kind == "image":
        stats = probe["images"]
        stats["count"] += 1
        stats["size_buckets"][facts.get("size_bucket", "?")] += 1
        stats["palette_buckets"][facts.get("palette_bucket", "?")] += 1
        if facts.get("pixel_art"):
            stats["pixel_art"] += 1
        stats["animated_frames"] += max(0, int(facts.get("frames") or 1) - 1)
        _track_sequence(agg, rel)
    elif kind == "audio":
        stats = probe["audio"]
        stats["count"] += 1
        role = facts.get("role") or "other"
        if role == "sfx":
            stats["sfx"] += 1
        elif role == "music":
            stats["music"] += 1
        else:
            stats["other"] += 1
        stats["total_duration"] += float(facts.get("duration") or 0.0)
    elif kind == "model3d":
        stats = probe["models_3d"]
        stats["count"] += 1
        stats["meshes"] += int(facts.get("meshes") or facts.get("objects") or 0)
        stats["animations"] += int(facts.get("animations") or 0)
    elif kind in ("unprobed", "skipped"):
        probe["unprobed"] += 1
    elif kind == "doc":
        pass  # licensing handled separately


def _track_sequence(agg: dict[str, Any], rel: str) -> None:
    directory, _, name = rel.rpartition("/")
    stem, dot, _ext = name.rpartition(".")
    if not dot:
        return
    m = _SEQUENCE.match(stem)
    if not m:
        return
    prefix = m.group("prefix").strip("_- ").lower()
    if not prefix:
        return
    agg["sequences"][(directory, prefix)].add(int(m.group("num")))


def _collect_doc(agg: dict[str, Any], rel: str, text: str) -> None:
    if len(agg["doc_texts"]) >= _MAX_DOCS:
        return
    if any(d["name"] == rel for d in agg["doc_texts"]):
        return
    agg["docs"].append(rel)
    agg["doc_texts"].append({"name": rel, "text": text})


def _probe_local_file(
    agg: dict[str, Any], abs_path: str, rel: str, size: int, mtime: float, cache: dict[str, Any], deep: bool
) -> None:
    cat = classify(rel)
    if deep and cat in _PROBE_CATEGORIES:
        if cat == "docs" and size > _MAX_DOC_PROBE:
            return
        facts = probe_path(abs_path, rel, size=size, mtime=mtime, cache=cache)
        _record_facts(agg, rel, facts)
        if facts.get("kind") == "doc":
            _collect_doc(agg, rel, facts.get("text") or "")
    elif cat == "docs" and is_probable_license_doc(rel) and size <= _MAX_DOC_BYTES:
        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            _collect_doc(agg, rel, text[:20000])
        except OSError:
            pass


def _scan_zip(
    agg: dict[str, Any], abs_path: str, rel_base: str, mtime: float, cache: dict[str, Any], deep: bool
) -> None:
    try:
        with zipfile.ZipFile(abs_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                if any(part in ("__MACOSX",) for part in name.split("/")) or is_junk_file(name):
                    continue
                rel = f"{rel_base}!/{name}"
                size = int(member.file_size)
                cat = _add_file(agg, rel, size)
                if deep and cat in _PROBE_CATEGORIES and size <= min(MAX_PROBE_BYTES, 20 * 1024 * 1024):
                    key = sha1(f"{rel}|{size}|{int(mtime)}")
                    facts = cache.get(key)
                    if facts is None:
                        try:
                            data = zf.read(member)
                        except (zipfile.BadZipFile, OSError, RuntimeError):
                            continue
                        facts = probe_bytes(name, data)
                        cache[key] = facts
                    _record_facts(agg, rel, facts)
                    if facts.get("kind") == "doc":
                        _collect_doc(agg, rel, facts.get("text") or "")
                elif cat == "docs" and is_probable_license_doc(name) and size <= _MAX_DOC_BYTES and deep is False:
                    try:
                        _collect_doc(agg, rel, zf.read(member)[:20000].decode("utf-8", "replace"))
                    except (zipfile.BadZipFile, OSError, RuntimeError, UnicodeDecodeError):
                        pass
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        warn(f"could not read archive {rel_base}: {exc}")


def _scan_tar(
    agg: dict[str, Any], abs_path: str, rel_base: str, mtime: float, cache: dict[str, Any], deep: bool
) -> None:
    try:
        with tarfile.open(abs_path) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = member.name.replace("\\", "/")
                if is_junk_file(name):
                    continue
                rel = f"{rel_base}!/{name}"
                size = int(member.size)
                cat = _add_file(agg, rel, size)
                if deep and cat in _PROBE_CATEGORIES and size <= min(MAX_PROBE_BYTES, 20 * 1024 * 1024):
                    fh = tf.extractfile(member)
                    if fh is None:
                        continue
                    data = fh.read()
                    key = sha1(f"{rel}|{size}|{int(mtime)}")
                    facts = cache.get(key) or probe_bytes(name, data)
                    cache[key] = facts
                    _record_facts(agg, rel, facts)
    except (tarfile.TarError, OSError) as exc:
        warn(f"could not read archive {rel_base}: {exc}")


def _scan_7z(agg: dict[str, Any], abs_path: str, rel_base: str) -> None:
    try:
        import py7zr
    except ImportError:
        warn(f"7z archive not inspected (install py7zr): {rel_base}")
        return
    try:
        with py7zr.SevenZipFile(abs_path, "r") as zf:
            for name in zf.getnames():
                if not name or name.endswith("/") or is_junk_file(name):
                    continue
                rel = f"{rel_base}!/{name.replace(chr(92), '/')}"
                _add_file(agg, rel, 0)
    except Exception as exc:
        warn(f"could not read 7z archive {rel_base}: {exc}")


def scan_folder(
    config: Config, folder: Path, *, deep: bool = True, cache: dict[str, Any] | None = None
) -> dict[str, Any]:
    assert config.paths is not None
    cache = cache if cache is not None else {}
    agg = _new_aggregate()
    files_seen = 0

    for abs_path, rel in _iter_download(folder, agg):
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        size = int(stat.st_size)
        files_seen += 1
        if files_seen % 500 == 0:
            info(f"    scanned {files_seen} entries ...")
        if is_archive(rel):
            agg["archive_count"] += 1
            _add_file(agg, rel, size)
            if rel.lower().endswith(".zip"):
                _scan_zip(agg, str(abs_path), rel, stat.st_mtime, cache, deep)
            elif rel.lower().endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
                _scan_tar(agg, str(abs_path), rel, stat.st_mtime, cache, deep)
            elif rel.lower().endswith(".7z"):
                _scan_7z(agg, str(abs_path), rel)
            continue
        _add_file(agg, rel, size)
        _probe_local_file(agg, str(abs_path), rel, size, stat.st_mtime, cache, deep)

    sequences = sum(1 for nums in agg["sequences"].values() if len(nums) >= 3)
    probe = agg["probe"]
    local = {
        "downloaded": True,
        "path": _project_rel(config, folder),
        "scanned_at": now_iso(),
        "file_count": agg["file_count"],
        "total_bytes": agg["total_bytes"],
        "total_human": human_bytes(agg["total_bytes"]),
        "categories": {
            cat: {"count": v["count"], "bytes": v["bytes"], "label": category_label(cat)}
            for cat, v in sorted(agg["categories"].items(), key=lambda kv: -kv[1]["count"])
        },
        "notable_files": {cat: paths for cat, paths in agg["notable"].items()},
        "manifest": agg["manifest"],
        "manifest_truncated": agg["manifest_truncated"],
        "animation_sequences": sequences,
        "archive_count": agg["archive_count"],
        "docs": agg["docs"],
        "probe": {
            "images": {**probe["images"], "size_buckets": dict(probe["images"]["size_buckets"]),
                       "palette_buckets": dict(probe["images"]["palette_buckets"])},
            "audio": {**probe["audio"], "total_duration": round(probe["audio"]["total_duration"], 1)},
            "models_3d": probe["models_3d"],
            "unprobed": probe["unprobed"],
        },
    }
    return {"local": local, "doc_texts": agg["doc_texts"]}


def _iter_download(folder: Path, agg: dict[str, Any]):
    import os

    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not is_junk_dir(d))
        for name in sorted(files):
            if is_junk_file(name):
                continue
            abs_path = Path(root) / name
            rel = abs_path.relative_to(folder).as_posix()
            yield abs_path, rel


def _project_rel(config: Config, folder: Path) -> str:
    try:
        return folder.resolve().relative_to(config.root).as_posix()
    except ValueError:
        return str(folder)


def run_scan(
    config: Config, *, deep: bool = True, force: bool = False, interactive: bool = True
) -> dict[str, int]:
    """Scan all matched download folders and write `local` back into the cards."""
    from .cards import load_cards, save_card, summarize
    from .licensing import build_attribution, credit_line

    assert config.paths is not None
    cards = load_cards(config.paths.cards)
    if not cards:
        warn(f"no asset cards found in {config.paths.cards}; run `python -m ledger ingest` first")
        return {"matched": 0, "scanned": 0}

    info(f"matching folders in {config.paths.downloads} ...")
    folder_map = match_folders(config, cards, interactive=interactive)

    cache_file = config.scan_cache / "probe.json"
    cache = {} if force else (read_json(cache_file, default={}) or {})

    scanned = 0
    for folder_name, slug in sorted(folder_map.items()):
        folder = config.paths.downloads / folder_name
        card = cards.get(slug)
        if card is None or not folder.is_dir():
            continue
        info(f"  scanning {folder_name} -> {slug}")
        result = scan_folder(config, folder, deep=deep, cache=cache)
        card["local"] = result["local"]
        card["local"]["folder"] = folder_name

        previous = (card.get("attribution") or {}).get("license") or {}
        fresh = build_attribution(
            card.get("itch") or {},
            description=card.get("description") or "",
            docs=result["doc_texts"],
        )
        # A pack with no license docs must not erase a license found on the page.
        if (fresh.get("license") or {}).get("type") == "unspecified" and previous.get("type") not in (None, "unspecified"):
            fresh["license"] = previous
            fresh["requires_attribution"] = (card.get("attribution") or {}).get("requires_attribution", "unknown")
            fresh["credit_line"] = credit_line(
                card.get("itch") or {}, previous.get("type") or "unspecified", fresh["requires_attribution"]
            )
        card["attribution"] = fresh
        card["summary"] = summarize(card)
        save_card(config, card)
        scanned += 1

    # Mark known assets without a local folder.
    matched_slugs = set(folder_map.values())
    for slug, card in cards.items():
        if slug in matched_slugs:
            continue
        local = card.get("local") or {}
        if local.get("downloaded"):
            card["local"] = {"downloaded": False, "path": None}
            card["summary"] = summarize(card)
            save_card(config, card)

    write_json(cache_file, cache)
    info(f"scan complete: {scanned} folders scanned")
    return {"matched": len(folder_map), "scanned": scanned}
