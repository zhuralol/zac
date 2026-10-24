"""itch.io server-side API access: owned keys + configured collections.

Reference: https://itch.io/docs/api/serverside
Auth: `Authorization: Bearer <API key>` (unscoped keys from /api-keys work).
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import urlencode

from .config import Config
from .util import HttpError, env_api_key, http_fetch, info, read_json, warn, write_json

API_BASE = "https://api.itch.io"
PAGE_SIZE = 500
MAX_PAGES = 500
ASSET_CLASSIFICATION = "assets"


class ItchAPI:
    def __init__(self, key: str | None = None, base: str = API_BASE):
        self.key = key or env_api_key()
        self.base = base.rstrip("/")

    # -- plumbing ---------------------------------------------------------
    def require_key(self) -> str:
        if not self.key:
            raise SystemExit(
                "ITCH_API_KEY is not set.\n"
                "Create an API key at https://itch.io/user/settings/api-keys and export it:\n"
                "  export ITCH_API_KEY=...\n"
                "(The key is only used for API calls and is never written into cards.)"
            )
        return self.key

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            status, _headers, body = http_fetch(
                url,
                headers={"Authorization": f"Bearer {self.require_key()}", "Accept": "application/json"},
            )
        except HttpError as exc:
            if exc.status in (401, 403):
                raise SystemExit(
                    f"itch.io rejected the API key (HTTP {exc.status}).\n"
                    "Check that the key is valid and not revoked: https://itch.io/user/settings/api-keys"
                ) from exc
            raise
        if status != 200:
            raise HttpError(url, status, body)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"itch.io returned non-JSON for {url}: {body[:200]!r}") from exc
        if isinstance(data, dict) and data.get("errors"):
            raise RuntimeError(f"itch.io API error for {path}: {data['errors']}")
        return data

    # -- endpoints --------------------------------------------------------
    def owned_keys(self) -> Iterable[dict[str, Any]]:
        page = 1
        while page <= MAX_PAGES:
            data = self.get("/profile/owned-keys", {"page": page, "per_page": PAGE_SIZE})
            keys = data.get("owned_keys") or []
            if not keys:
                return
            yield from keys
            page += 1

    def collections(self) -> list[dict[str, Any]]:
        data = self.get("/profile/collections")
        return data.get("collections") or []

    def collection_games(self, collection_id: int | str) -> Iterable[dict[str, Any]]:
        page = 1
        while page <= MAX_PAGES:
            data = self.get(
                f"/collections/{collection_id}/collection-games",
                {"page": page, "per_page": PAGE_SIZE},
            )
            entries = data.get("collection_games") or []
            if not entries:
                return
            yield from entries
            page += 1


# -- shaping -------------------------------------------------------------
def _author_of(game: dict[str, Any]) -> tuple[str | None, str | None]:
    user = game.get("user") or {}
    return user.get("display_name") or user.get("username"), user.get("url")


def _acquired_from_key(key: dict[str, Any]) -> str:
    if key.get("purchase_id"):
        return "purchased"
    # Download keys without a purchase come from bundle claims / free claims / gifts.
    return "claimed"


def _base_record(game: dict[str, Any]) -> dict[str, Any]:
    author, author_url = _author_of(game)
    return {
        "game_id": game.get("id"),
        "title": game.get("title") or "Untitled",
        "url": (game.get("url") or "").replace("http://", "https://", 1),
        "author": author,
        "author_url": author_url,
        "classification": game.get("classification") or "unknown",
        "cover_url": game.get("cover_url"),
        "published_at": game.get("published_at"),
        "created_at": game.get("created_at"),
        "min_price": game.get("min_price"),
        "traits": game.get("traits") or [],
        "acquired": "unknown",
        "owned_key": False,
        "sources": [],
        "download_key": None,
    }


def _allowed(classification: str | None, allowlist: list[str], from_collection: bool) -> bool:
    cls = (classification or "").lower()
    if cls in allowlist:
        return True
    # Collections are curated by the user; some endpoints omit `classification`.
    # Treat those as assets rather than silently dropping free assets, but say so.
    return from_collection and not cls


def collect_assets(config: Config, api: ItchAPI | None = None) -> tuple[dict[str, dict], dict[str, int]]:
    """Merge owned keys and configured collections into one asset inventory.

    Returns (records keyed by game_id, rejection counts by classification).
    """
    api = api or ItchAPI()
    api.require_key()
    allow = config.classifications or [ASSET_CLASSIFICATION]
    records: dict[str, dict] = {}
    rejected: dict[str, int] = defaultdict(int)
    unknown_in_collections = 0

    # 1. Owned keys (purchases, bundles, claims, gifts) -------------------
    info("fetching owned keys ...")
    owned_count = 0
    for key in api.owned_keys():
        game = key.get("game") or {}
        if not game.get("id"):
            continue
        owned_count += 1
        cls = game.get("classification") or "unknown"
        if not _allowed(cls, allow, from_collection=False):
            rejected[cls] += 1
            continue
        gid = str(game["id"])
        rec = records.get(gid)
        if rec is None:
            rec = _base_record(game)
            rec["acquired"] = _acquired_from_key(key)
            rec["owned_key"] = True
            rec["download_key"] = key.get("key")
            rec["sources"] = ["owned-keys"]
            records[gid] = rec
        else:
            rec["owned_key"] = True
            rec["download_key"] = rec.get("download_key") or key.get("key")
        if not game.get("classification"):
            rec["classification"] = "unknown"
    info(f"owned keys seen: {owned_count}")

    # 2. Configured collections (covers free assets, which have no key) ---
    if config.collections:
        info(f"fetching collections: {', '.join(config.collections)} ...")
        available = api.collections()
        by_name = {str(c.get("title", "")).strip().lower(): c for c in available}
        by_id = {str(c.get("id")): c for c in available}
        matched: list[dict[str, Any]] = []
        for name in config.collections:
            found = by_name.get(name.strip().lower()) or by_id.get(name.strip())
            if not found:
                warn(f"collection not found: {name!r} (run `python -m zac collections`)")
                continue
            matched.append(found)
            info(f"  using collection {found.get('id')}: {found.get('title')}")

        for coll in matched:
            for entry in api.collection_games(coll["id"]):
                game = entry.get("game") or {}
                if not game.get("id"):
                    continue
                cls = game.get("classification")
                if not _allowed(cls, allow, from_collection=True):
                    rejected[cls or "unknown"] += 1
                    continue
                if not cls:
                    unknown_in_collections += 1
                gid = str(game["id"])
                source = f"collection:{coll.get('title')}"
                rec = records.get(gid)
                if rec is None:
                    rec = _base_record(game)
                    rec["acquired"] = "free"
                    rec["sources"] = [source]
                    records[gid] = rec
                elif source not in rec["sources"]:
                    rec["sources"].append(source)
                # Keep the richer owned-keys data if both sources saw the game.
                rec["url"] = rec.get("url") or (game.get("url") or "").replace("http://", "https://", 1)
                if not rec.get("cover_url"):
                    rec["cover_url"] = game.get("cover_url")
    else:
        info("no collections configured (config.toml `collections`); free assets will be missed")

    if unknown_in_collections:
        warn(
            f"{unknown_in_collections} collection entries had no classification; "
            "kept as assets because the collection is user-curated"
        )
    return records, dict(rejected)


def save_download_keys(config: Config, records: dict[str, dict]) -> int:
    """Persist download keys separately (gitignored), never inside cards."""
    if not config.paths:
        return 0
    existing = read_json(config.paths.keys, default={}) or {}
    changed = 0
    for gid, rec in records.items():
        key = rec.get("download_key")
        if not key:
            continue
        entry = {
            "title": rec.get("title"),
            "url": rec.get("url"),
            "download_url": _download_url(rec, key),
            "key": key,
        }
        if existing.get(gid) != entry:
            existing[gid] = entry
            changed += 1
    if changed:
        write_json(config.paths.keys, existing)
    return changed


def _download_url(rec: dict[str, Any], key: str) -> str | None:
    url = rec.get("url") or ""
    if not url:
        return None
    return f"{url.rstrip('/')}/download/{key}"
