"""Offline smoke tests: licensing, matching, scanning, rendering."""

from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from PIL import Image

from zac.cards import load_cards
from zac.config import load_config
from zac.licensing import build_attribution, detect_license
from zac.render import render_all
from zac.scanner import match_score, run_scan
from zac.taxonomy import classify, is_junk_file
from zac.util import normalize_name, slugify

TITLE = "Pixel Necro Pack v2"
AUTHOR = "Pixel Poison"
URL = "https://pixelpoison.itch.io/pixel-necro-pack"


def make_png(path: Path, size: tuple[int, int], palette: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("P", size)
    # Build a small palette so the pixel-art heuristic sees few colors.
    im.putpalette([(i * 8) % 256 for i in range(768)])
    im.putdata([i % palette for i in range(size[0] * size[1])])
    im.save(path)


def make_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        data = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames)
        )
        fh.writeframes(data)


class LicenseTests(unittest.TestCase):
    def test_cc_by_version(self) -> None:
        found = detect_license("This pack is licensed under CC BY 4.0, see the file.")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["type"], "CC BY 4.0")
        self.assertEqual(found["requires_attribution"], "yes")

    def test_cc0_beats_cc_by(self) -> None:
        found = detect_license("Released under CC0. Also mentions Creative Commons Attribution for unrelated text.")
        assert found is not None
        self.assertIn("CC0", found["type"])

    def test_mit_and_none(self) -> None:
        assert detect_license("MIT License\nCopyright (c) 2024")["type"] == "MIT"  # type: ignore[index]
        self.assertIsNone(detect_license("A pack of sprites for your game."))

    def test_docs_beat_page_and_set_confidence(self) -> None:
        attr = build_attribution(
            {"title": TITLE, "author": AUTHOR, "author_url": URL},
            description="royalty free",
            docs=[{"name": "LICENSE.txt", "text": "Licensed under CC BY 4.0."}],
        )
        self.assertEqual(attr["license"]["type"], "CC BY 4.0")
        self.assertEqual(attr["license"]["confidence"], "high")
        self.assertEqual(attr["license"]["source"], "LICENSE.txt")
        self.assertIn(AUTHOR, attr["credit_line"])

    def test_unspecified_is_flagged(self) -> None:
        attr = build_attribution({"title": "X", "author": "Y"})
        self.assertEqual(attr["license"]["type"], "unspecified")
        self.assertEqual(attr["requires_attribution"], "unknown")


class TaxonomyTests(unittest.TestCase):
    def test_categories(self) -> None:
        self.assertEqual(classify("sprites/a.png"), "images")
        self.assertEqual(classify("music/theme.ogg"), "audio")
        self.assertEqual(classify("model.gltf"), "models_3d")
        self.assertEqual(classify("art/my.aseprite"), "source_projects")
        self.assertEqual(classify("LICENSE.txt"), "docs")
        self.assertTrue(is_junk_file(".DS_Store"))

    def test_normalize_and_slug(self) -> None:
        self.assertEqual(normalize_name("Pixel Necro Pack v2"), normalize_name("pixel-necro-pack"))
        self.assertEqual(slugify("Pixel Necro Pack v2"), "pixel-necro-pack")


class EndToEndTests(unittest.TestCase):
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
        assert self.cfg.paths is not None
        self.cfg.paths.cards.mkdir(parents=True)

        card = {
            "slug": "pixel-necro-pack",
            "itch": {
                "game_id": 1,
                "title": TITLE,
                "url": URL,
                "author": AUTHOR,
                "author_url": "https://pixelpoison.itch.io",
                "classification": "assets",
                "acquired": "purchased",
                "owned_key": True,
                "sources": ["owned-keys"],
            },
            "description": "Necromancer sprites for dark fantasy games.",
            "tags": ["pixel art", "dark fantasy", "necromancer"],
            "attribution": {"license": {"type": "unspecified"}},
            "local": {"downloaded": False, "path": None},
            "summary": "",
        }
        (self.cfg.paths.cards / "pixel-necro-pack.json").write_text(json.dumps(card), encoding="utf-8")

        other = dict(card)
        other["slug"] = "undownloaded-pack"
        other["itch"] = dict(card["itch"], game_id=2, title="Undownloaded Pack", url="https://x.itch.io/undownloaded")
        (self.cfg.paths.cards / "undownloaded-pack.json").write_text(json.dumps(other), encoding="utf-8")

        folder = self.cfg.paths.downloads / "Pixel Necro Pack v2"
        make_png(folder / "sprites" / "enemy_idle_01.png", (16, 16))
        make_png(folder / "sprites" / "enemy_idle_02.png", (16, 16))
        make_png(folder / "sprites" / "enemy_idle_03.png", (16, 16))
        make_png(folder / "sprites" / "tile_01.png", (32, 32))
        make_png(folder / "sprites" / "background.png", (256, 256))
        make_wav(folder / "audio" / "hit.wav", 0.5)
        make_wav(folder / "audio" / "theme.wav", 40.0)
        (folder / "LICENSE.txt").write_text("Pixel Necro Pack by Pixel Poison.\nLicensed under CC BY 4.0.", encoding="utf-8")
        (folder / "README.md").write_text("# Pixel Necro Pack\nSprites and audio.", encoding="utf-8")
        (folder / "__MACOSX").mkdir()
        (folder / "__MACOSX" / "junk.png").write_bytes(b"not really a png")
        (folder / ".DS_Store").write_bytes(b"junk")
        with zipfile.ZipFile(folder / "extra.zip", "w") as zf:
            for i in (1, 2):
                buf = Path(self.root / f"z{i}.png")
                make_png(buf, (24, 24))
                zf.write(buf, f"bonus/bonus_{i:02d}.png")
                buf.unlink()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_matching(self) -> None:
        card = load_cards(self.cfg.paths.cards)["pixel-necro-pack"]  # type: ignore[union-attr]
        self.assertGreater(match_score("Pixel Necro Pack v2", card), 0.9)
        self.assertLess(match_score("Totally Different Audio Bundle", card), 0.6)  # type: ignore[arg-type]

    def test_scan_and_render(self) -> None:
        stats = run_scan(self.cfg, deep=True, force=True, interactive=False)
        self.assertEqual(stats["scanned"], 1)

        cards = load_cards(self.cfg.paths.cards)
        card = cards["pixel-necro-pack"]
        local = card["local"]
        self.assertTrue(local["downloaded"])
        self.assertGreaterEqual(local["categories"]["images"]["count"], 7)  # 5 loose + 2 in zip
        self.assertEqual(local["categories"]["audio"]["count"], 2)
        self.assertEqual(local["categories"]["docs"]["count"], 2)
        self.assertGreaterEqual(local["animation_sequences"], 1)  # enemy_idle_01..03
        self.assertGreater(local["probe"]["images"]["pixel_art"], 0)
        self.assertEqual(local["probe"]["audio"]["sfx"], 1)
        self.assertEqual(local["probe"]["audio"]["music"], 1)

        attr = card["attribution"]
        self.assertEqual(attr["license"]["type"], "CC BY 4.0")
        self.assertEqual(attr["license"]["confidence"], "high")
        self.assertIn(AUTHOR, attr["credit_line"])

        self.assertFalse(cards["undownloaded-pack"]["local"]["downloaded"])

        render_all(self.cfg)
        assets_md = (self.cfg.paths.assets_md).read_text(encoding="utf-8")
        attribution = (self.cfg.paths.attribution_md).read_text(encoding="utf-8")
        self.assertIn(TITLE, assets_md)
        self.assertIn("pixel-art", assets_md)
        self.assertIn("Undownloaded Pack", assets_md)
        self.assertIn("metadata only", assets_md.lower())
        self.assertIn("CC BY 4.0", attribution)
        self.assertIn(AUTHOR, attribution)
        self.assertNotIn("download key", attribution.lower())

    def test_scan_is_idempotent_and_cached(self) -> None:
        run_scan(self.cfg, deep=True, force=True, interactive=False)
        first = load_cards(self.cfg.paths.cards)["pixel-necro-pack"]["local"]["probe"]
        run_scan(self.cfg, deep=True, force=False, interactive=False)
        second = load_cards(self.cfg.paths.cards)["pixel-necro-pack"]["local"]["probe"]
        self.assertEqual(first, second)
        self.assertTrue((self.cfg.scan_cache / "probe.json").exists())


if __name__ == "__main__":
    unittest.main()
