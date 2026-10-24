"""Tests for sprite-sheet detection and per-file cataloguing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from zac.config import load_config
from zac.filedesc import (
    _parse_json,
    build_items,
    fallback_description,
    fill_nonimage_descriptions,
)
from zac.render import render_files_md
from zac.sheets import annotate_grid, detect_grid, sheet_kind


def make_sheet(path: Path, cols: int = 6, rows: int = 2, cell: int = 32) -> None:
    """Transparent sheet with a sprite inset in each cell (4px gutters)."""
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    colors = [(220, 40, 40), (40, 200, 60), (60, 80, 230), (240, 200, 40),
              (200, 60, 200), (40, 200, 200), (250, 130, 20), (120, 60, 200),
              (30, 140, 70), (200, 200, 200), (100, 30, 30), (30, 30, 120)]
    for r in range(rows):
        for c in range(cols):
            x, y = c * cell + 4, r * cell + 4
            draw.rectangle([x, y, x + cell - 9, y + cell - 9], fill=colors[(r * cols + c) % len(colors)])
    sheet.save(path)


def make_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (16, 16)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


class SheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detects_uniform_grid(self) -> None:
        path = self.root / "sheet.png"
        make_sheet(path)
        grid = detect_grid(path)
        self.assertIsNotNone(grid)
        assert grid is not None
        self.assertEqual((grid["cols"], grid["rows"]), (6, 2))
        self.assertEqual((grid["cell_w"], grid["cell_h"]), (32, 32))
        self.assertEqual(grid["cells"], 12)
        self.assertEqual(sheet_kind(path, grid), "atlas")

    def test_annotation_keeps_source_coordinates(self) -> None:
        path = self.root / "sheet.png"
        make_sheet(path)
        grid = detect_grid(path)
        assert grid is not None
        image, cells = annotate_grid(path, grid)
        self.assertEqual(len(cells), 12)
        cell = cells[7]  # row 1, col 1
        self.assertEqual((cell["row"], cell["col"]), (1, 1))
        self.assertEqual((cell["x"], cell["y"]), (32, 32))
        self.assertEqual((cell["w"], cell["h"]), (32, 32))
        self.assertGreater(image.width, 0)

    def test_opaque_image_is_not_a_sheet(self) -> None:
        path = self.root / "art.png"
        Image.new("RGB", (512, 512), (120, 90, 60)).save(path)
        self.assertIsNone(detect_grid(path))


class FileDescTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset = self.root / "downloads" / "pack"
        for i in range(10):
            make_png(self.asset / "frames" / f"frame{i:04d}.png", (i * 20, 30, 40))
        make_png(self.asset / "standalone.png", (10, 200, 10), (64, 64))
        self.card = {
            "slug": "pack",
            "itch": {"title": "Pack"},
            "tags": [],
            "local": {
                "downloaded": True,
                "path": str(self.asset),
                "manifest": [
                    {
                        "path": f"frames/frame{i:04d}.png",
                        "category": "images",
                        "size": 100,
                        "facts": {"width": 16, "height": 16},
                    }
                    for i in range(10)
                ] + [
                    {
                        "path": "standalone.png",
                        "category": "images",
                        "size": 200,
                        "facts": {"width": 64, "height": 64},
                    },
                    {
                        "path": "sound.wav",
                        "category": "audio",
                        "size": 300,
                        "facts": {"kind": "audio", "role": "sfx", "duration": 0.4},
                    },
                ],
            },
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sequences_are_grouped(self) -> None:
        items = build_items(self.card)
        sequences = [i for i in items if i.kind == "sequence"]
        singles = [i for i in items if i.kind == "single"]
        self.assertEqual(len(sequences), 1)
        self.assertEqual(len(sequences[0].files), 10)
        self.assertEqual(len(sequences[0].samples), 3)
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0].files[0]["path"], "standalone.png")

    def test_fallback_description_for_audio(self) -> None:
        entry = self.card["local"]["manifest"][-1]
        text = fallback_description(entry)
        self.assertIn("Sound effect", text or "")
        self.assertIn("0.40s", text or "")

    def test_fill_nonimage_descriptions(self) -> None:
        count = fill_nonimage_descriptions(self.card)
        self.assertEqual(count, 1)
        self.assertIn("Sound effect", self.card["local"]["manifest"][-1]["description"])

    def test_parse_json_from_fence(self) -> None:
        data = _parse_json('```json\n{"0": "sword", "1": "axe"}\n```')
        self.assertEqual(data, {"0": "sword", "1": "axe"})
        self.assertIsNone(_parse_json("no json here"))

    def test_render_file_catalogue(self) -> None:
        (self.root / "config.toml").write_text(
            "\n".join(
                [
                    "[paths]",
                    'cards = "assets/cards"',
                    'assets_md = "ASSETS.md"',
                    'attribution_md = "ATTRIBUTION.md"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(self.root / "config.toml"), create=False)
        assert cfg.paths is not None
        cfg.paths.cards.mkdir(parents=True)
        self.card["local"]["manifest"][0]["description"] = "Skeleton walk cycle"
        (cfg.paths.cards / "pack.json").write_text(json.dumps(self.card), encoding="utf-8")
        out = render_files_md(cfg)
        text = (out / "pack.md").read_text(encoding="utf-8")
        self.assertIn("frames/frame0000.png", text)
        self.assertIn("Skeleton walk cycle", text)


if __name__ == "__main__":
    unittest.main()
