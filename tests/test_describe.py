"""Tests for image description (contact sheets + vision backends)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from zac.cards import load_cards
from zac.config import load_config
from zac.describe import (
    VisionSettings,
    build_contact_sheet,
    describe_card,
    sample_images,
    set_description,
)
from zac.render import render_all


def make_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


class SheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset = self.root / "downloads" / "pack"
        for d in ("enemies", "tiles", "ui"):
            for i in range(10):
                make_png(self.asset / d / f"{d}_{i:02d}.png", (i * 20, 40, 60))
        self.card = {
            "slug": "pack",
            "itch": {"title": "Pack", "author": "Artist"},
            "tags": ["pixel art"],
            "local": {"downloaded": True, "path": str(self.asset)},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_samples_spread_across_folders(self) -> None:
        samples = sample_images(self.card, 6)
        self.assertEqual(len(samples), 6)
        parents = {rel.rsplit("/", 1)[0] for _, rel in samples}
        self.assertEqual(parents, {"enemies", "tiles", "ui"})
        # samples come from the middle of each sequence, not blank first frames
        self.assertIn("enemies/enemies_03.png", {rel for _, rel in samples})

    def test_contact_sheet_is_labeled(self) -> None:
        sheet, meta = build_contact_sheet(self.card, 6)
        self.assertEqual(len(meta), 6)
        self.assertEqual(sheet.width, 6 * 190)
        self.assertEqual(sheet.height, 210)  # one row + label strip
        self.assertTrue(all("index" in m and "path" in m for m in meta))


class FakeVision(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/chat/completions":
            content = payload["messages"][0]["content"]
            assert any(part.get("type") == "image_url" for part in content if isinstance(part, dict))
            body = {"choices": [{"message": {"content": "Pixel-art slimes and dungeon tiles."}}]}
        elif self.path == "/api/chat":
            assert payload["messages"][0]["images"]
            body = {"message": {"content": "Pixel-art slimes and dungeon tiles."}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVision)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text(
            "\n".join(
                [
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
        assert self.cfg.paths is not None
        self.asset = self.root / "downloads" / "pack"
        for i in range(6):
            make_png(self.asset / f"sprite_{i:02d}.png", (10 * i, 50, 90))
        card = {
            "slug": "pack",
            "itch": {"title": "Slime Pack", "author": "Artist", "game_id": 1},
            "description": "",
            "tags": ["pixel art"],
            "attribution": {"license": {"type": "CC0"}, "requires_attribution": "no", "credit_line": "x"},
            "local": {"downloaded": True, "path": str(self.asset), "file_count": 6},
            "summary": "six sprites",
        }
        self.cfg.paths.cards.mkdir(parents=True)
        (self.cfg.paths.cards / "pack.json").write_text(json.dumps(card), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_openai_backend(self) -> None:
        card = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        settings = VisionSettings(provider="openai", model="test-vl", base_url=f"{self.base}/v1", api_key="k")
        text = describe_card(self.cfg, card, settings, limit=6)
        self.assertIn("slimes", text or "")
        saved = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        self.assertEqual(saved["content"]["description"], "Pixel-art slimes and dungeon tiles.")
        self.assertEqual(saved["content"]["model"], "openai:test-vl")
        self.assertEqual(saved["content"]["samples"], 6)

    def test_ollama_backend(self) -> None:
        card = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        settings = VisionSettings(provider="ollama", model="llava", base_url=self.base)
        describe_card(self.cfg, card, settings, limit=4)
        saved = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        self.assertEqual(saved["content"]["model"], "ollama:llava")

    def test_set_description_renders(self) -> None:
        card = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        set_description(self.cfg, card, "Cute slimes in three colours.")
        render_all(self.cfg)
        assets_md = self.cfg.paths.assets_md.read_text()  # type: ignore[union-attr]
        self.assertIn("**Looks like:** Cute slimes in three colours.", assets_md)

    def test_describe_skips_existing_unless_forced(self) -> None:
        card = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        settings = VisionSettings(provider="openai", model="test-vl", base_url=f"{self.base}/v1", api_key="k")
        self.assertIsNotNone(describe_card(self.cfg, card, settings, limit=4))
        card = load_cards(self.cfg.paths.cards)["pack"]  # type: ignore[union-attr]
        self.assertIsNone(describe_card(self.cfg, card, settings, limit=4))


if __name__ == "__main__":
    unittest.main()
