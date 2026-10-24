"""Tests for the bundled zac-assets skill query script."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "zac-assets" / "scripts" / "find_assets.py"


def _card(slug: str, **overrides):
    card = {
        "slug": slug,
        "itch": {
            "title": slug.replace("-", " ").title(),
            "author": "Test Artist",
            "url": f"https://test.itch.io/{slug}",
            "acquired": "purchased",
        },
        "tags": [],
        "attribution": {
            "license": {"type": "CC BY 4.0"},
            "requires_attribution": "yes",
            "credit_line": f"{slug} by Test Artist — CC BY 4.0",
        },
        "local": {"downloaded": False, "path": None},
        "summary": "",
    }
    card.update(overrides)
    return card


class FindAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cards_dir = self.root / "assets" / "cards"
        cards_dir.mkdir(parents=True)
        pixel = _card(
            "pixel-pack",
            tags=["pixel art", "dark fantasy", "necromancer"],
            local={
                "downloaded": True,
                "path": "downloads/pixel-pack",
                "file_count": 120,
                "total_human": "1.2 MB",
                "categories": {"images": {"count": 100, "bytes": 1000, "label": "sprites/textures"}},
                "probe": {"images": {"pixel_art": 90}},
            },
        )
        tiles = _card(
            "free-tiles",
            tags=["tileset", "top-down"],
            attribution={
                "license": {"type": "unspecified"},
                "requires_attribution": "unknown",
                "credit_line": "Free Tiles by Test Artist — license unspecified",
            },
        )
        pixel["attribution"]["credit_line"] = "Pixel Pack by Test Artist — CC BY 4.0"
        audio = _card(
            "ambient-audio",
            tags=["music", "ambient"],
            attribution={
                "license": {"type": "CC0 / Public Domain"},
                "requires_attribution": "no",
                "credit_line": "Ambient Audio by Test Artist — CC0 / Public Domain",
            },
        )
        for card in (pixel, tiles, audio):
            (cards_dir / f"{card['slug']}.json").write_text(json.dumps(card), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
        )

    def test_filters_by_tag_and_category(self) -> None:
        result = self.run_script("--tag", "pixel-art", "--category", "images", "--downloaded")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pixel Pack", result.stdout)
        self.assertIn("License: CC BY 4.0", result.stdout)
        self.assertNotIn("Free Tiles", result.stdout)
        self.assertNotIn("Ambient Audio", result.stdout)

    def test_missing_assets_are_flagged(self) -> None:
        result = self.run_script("--missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ambient Audio", result.stdout)
        self.assertIn("NOT downloaded", result.stdout)
        self.assertIn("contents unknown", result.stdout)

    def test_credits_mode(self) -> None:
        result = self.run_script("--credits")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pixel Pack by Test Artist — CC BY 4.0", result.stdout)
        self.assertIn("CC0 / Public Domain", result.stdout)

    def test_json_output(self) -> None:
        result = self.run_script("--json", "tileset")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["slug"], "free-tiles")
        self.assertEqual(data[0]["license"], "unspecified")
        self.assertFalse(data[0]["downloaded"])

    def test_no_match_and_bad_root(self) -> None:
        no_match = self.run_script("definitely-not-an-asset")
        self.assertEqual(no_match.returncode, 1)
        bad_root = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root / "nope")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad_root.returncode, 2)
        self.assertIn("No ZAC inventory found", bad_root.stderr)


if __name__ == "__main__":
    unittest.main()
