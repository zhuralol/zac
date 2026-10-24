"""Ingest tests against a local mock of the itch.io API (no network)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from zac.cards import load_cards
from zac.config import load_config
from zac.itch_api import ItchAPI, collect_assets, save_download_keys
from zac.cards import merge_ingest

OWNED = {
    "page": 1,
    "per_page": 500,
    "owned_keys": [
        {
            "id": 1,
            "game_id": 101,
            "purchase_id": 55,
            "key": "SECRETKEY-ASSET",
            "game": {
                "id": 101,
                "title": "Bought Asset Pack",
                "url": "https://artist.itch.io/bought-asset-pack",
                "classification": "assets",
                "user": {"display_name": "Artist", "url": "https://artist.itch.io"},
            },
        },
        {
            "id": 2,
            "game_id": 102,
            "purchase_id": None,
            "key": "SECRETKEY-CLAIM",
            "game": {
                "id": 102,
                "title": "Claimed Bundle Asset",
                "url": "https://artist.itch.io/claimed-asset",
                "classification": "assets",
                "user": {"display_name": "Artist", "url": "https://artist.itch.io"},
            },
        },
        {
            "id": 3,
            "game_id": 103,
            "purchase_id": 56,
            "key": "SECRETKEY-GAME",
            "game": {
                "id": 103,
                "title": "A Video Game",
                "url": "https://dev.itch.io/a-game",
                "classification": "game",
                "user": {"display_name": "Dev", "url": "https://dev.itch.io"},
            },
        },
    ],
}

COLLECTIONS = {"collections": [{"id": 10, "title": "My Assets", "games_count": 3, "private": False}]}

COLLECTION_GAMES = {
    "page": 1,
    "per_page": 500,
    "collection_games": [
        {
            "game_id": 201,
            "game": {
                "id": 201,
                "title": "Free Asset Pack",
                "url": "https://free.itch.io/free-pack",
                "classification": "assets",
                "user": {"display_name": "FreeDev", "url": "https://free.itch.io"},
            },
        },
        {
            "game_id": 202,
            "game": {
                "id": 202,
                "title": "A Game In My Collection",
                "url": "https://dev.itch.io/collection-game",
                "classification": "game",
                "user": {"display_name": "Dev", "url": "https://dev.itch.io"},
            },
        },
        {
            "game_id": 203,
            "game": {
                "id": 203,
                "title": "Classless Asset",
                "url": "https://free.itch.io/classless",
                "user": {"display_name": "FreeDev", "url": "https://free.itch.io"},
            },
        },
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        page = int((parse_qs(parsed.query).get("page") or ["1"])[0])
        if self.headers.get("Authorization") != "Bearer TESTKEY":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"errors":["unauthorized"]}')
            return
        if parsed.path == "/profile/owned-keys":
            body = OWNED if page == 1 else {"page": page, "per_page": 500, "owned_keys": []}
        elif parsed.path == "/profile/collections":
            body = COLLECTIONS
        elif parsed.path == "/collections/10/collection-games":
            body = COLLECTION_GAMES if page == 1 else {"page": page, "collection_games": []}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.toml").write_text(
            "\n".join(
                [
                    'collections = ["My Assets"]',
                    'classifications = ["assets"]',
                    "[paths]",
                    'downloads = "downloads"',
                    'cards = "assets/cards"',
                    'cache = ".cache"',
                    'keys = "keys.json"',
                    'matches = "assets/matches.json"',
                    'assets_md = "ASSETS.md"',
                    'attribution_md = "ATTRIBUTION.md"',
                ]
            ),
            encoding="utf-8",
        )
        self.cfg = load_config(str(root / "config.toml"), create=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_filters_games_and_merges_collections(self) -> None:
        api = ItchAPI(key="TESTKEY", base=self.base)
        records, rejected = collect_assets(self.cfg, api)
        ids = set(records)
        self.assertEqual(ids, {"101", "102", "201", "203"})
        self.assertEqual(rejected.get("game"), 2)  # owned game + collection game
        self.assertEqual(records["101"]["acquired"], "purchased")
        self.assertEqual(records["102"]["acquired"], "claimed")
        self.assertEqual(records["201"]["acquired"], "free")
        self.assertFalse(records["201"]["owned_key"])
        self.assertTrue(records["101"]["owned_key"])

        merge_ingest(self.cfg, records)
        cards = load_cards(self.cfg.paths.cards)  # type: ignore[union-attr]
        self.assertEqual(len(cards), 4)
        titles = {c["itch"]["title"] for c in cards.values()}
        self.assertIn("Free Asset Pack", titles)
        self.assertNotIn("A Video Game", titles)

        save_download_keys(self.cfg, records)
        keys_text = (self.cfg.paths.keys).read_text()  # type: ignore[union-attr]
        self.assertIn("SECRETKEY-ASSET", keys_text)
        for card in cards.values():
            self.assertNotIn("SECRETKEY", json.dumps(card))

    def test_bad_key_is_rejected(self) -> None:
        api = ItchAPI(key="WRONG", base=self.base)
        with self.assertRaises(SystemExit):
            collect_assets(self.cfg, api)


if __name__ == "__main__":
    unittest.main()
