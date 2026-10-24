"""Tests for local (no-API) card bootstrapping via `import-dir`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zac.cards import load_cards, merge_ingest
from zac.config import load_config
from zac.importer import import_directory


class ImportDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text(
            "\n".join(
                [
                    "collections = []",
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
        self.cfg = load_config(str(self.root / "config.toml"), create=False)
        self.downloads = self.root / "external-downloads"
        for name, files in {
            "Pixel Necro Pack v2": ["a.png", "b.png"],
            "400 Sounds Pack": ["hit.wav"],
            "Empty Folder": [],
        }.items():
            folder = self.downloads / name
            folder.mkdir(parents=True)
            for f in files:
                (folder / f).write_bytes(b"data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creates_local_cards_and_skips_existing(self) -> None:
        stats = import_directory(self.cfg, self.downloads)
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["empty"], 1)
        cards = load_cards(self.cfg.paths.cards)  # type: ignore[union-attr]
        self.assertEqual(set(cards), {"pixel-necro-pack", "400-sounds-pack"})
        for card in cards.values():
            self.assertTrue(str(card["itch"]["game_id"]).startswith("local-"))
            self.assertEqual(card["itch"]["acquired"], "local")
            self.assertIn("local-import", card["itch"]["sources"])
            self.assertFalse(card["local"]["downloaded"])

        again = import_directory(self.cfg, self.downloads)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped"], 2)

    def test_ingest_adopts_local_card(self) -> None:
        import_directory(self.cfg, self.downloads)
        record = {
            "game_id": 555,
            "title": "Pixel Necro Pack v2",
            "url": "https://artist.itch.io/pixel-necro-pack-v2",
            "author": "Artist",
            "author_url": "https://artist.itch.io",
            "classification": "assets",
            "acquired": "purchased",
            "owned_key": True,
            "sources": ["owned-keys"],
            "download_key": None,
        }
        merge_ingest(self.cfg, {"555": record})
        cards = load_cards(self.cfg.paths.cards)  # type: ignore[union-attr]
        self.assertEqual(len(cards), 2)  # no duplicate card
        adopted = cards["pixel-necro-pack"]
        self.assertEqual(adopted["itch"]["game_id"], 555)
        self.assertEqual(adopted["itch"]["acquired"], "purchased")
        self.assertTrue(adopted["itch"]["owned_key"])


if __name__ == "__main__":
    unittest.main()
