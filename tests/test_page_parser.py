"""Parser tests using saved HTML (no network)."""

from __future__ import annotations

import unittest

from zac.page_parser import html_to_markdown, parse_page

PAGE = """
<html><head>
<meta property="og:image" content="https://img.itch.zone/cover.png">
<meta property="og:title" content="Pixel Necro Pack by Pixel Poison">
</head><body>
<a class="user_title" href="https://pixelpoison.itch.io">Pixel Poison</a>
<h1 class="game_title">Pixel Necro Pack</h1>
<div class="game_tags">
  <a class="tag" href="/tag-pixel-art">pixel art</a>
  <a class="tag" href="/tag-dark-fantasy">dark fantasy</a>
</div>
<div class="formatted_description">
  <p>Necromancer <strong>sprites</strong> and <em>audio</em>.</p>
  <ul><li>16x16 enemies</li><li>Loops</li></ul>
  <script>alert('do not execute')</script>
  <p>Licensed under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.</p>
</div>
</body></html>
"""


class PageParserTests(unittest.TestCase):
    def test_parse(self) -> None:
        parsed = parse_page(PAGE, "https://pixelpoison.itch.io/pixel-necro-pack")
        self.assertEqual(parsed["title"], "Pixel Necro Pack")
        self.assertEqual(parsed["author"], "Pixel Poison")
        self.assertEqual(parsed["tags"], ["pixel art", "dark fantasy"])
        self.assertEqual(parsed["cover_url"], "https://img.itch.zone/cover.png")
        self.assertIn("**sprites**", parsed["description"])
        self.assertIn("- 16x16 enemies", parsed["description"])
        self.assertIn("CC BY 4.0", parsed["description"])
        self.assertNotIn("alert", parsed["description"])
        self.assertNotIn("<p>", parsed["description"])

    def test_markdown_drops_scripts(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<div><p>ok</p><script>bad()</script></div>", "html.parser")
        out = html_to_markdown(soup.select_one("div"))
        self.assertEqual(out, "ok")


if __name__ == "__main__":
    unittest.main()
