"""Fetch and parse public itch.io game/asset pages.

Page content is treated strictly as data: scripts/styles/iframes are removed and
nothing from the page is ever executed or interpreted as instructions.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from .util import HttpError, info, read_json, sha1, write_json

POLITE_DELAY = 1.0
_BLOCK_TAGS = {
    "p", "div", "br", "section", "article", "header", "footer", "blockquote",
    "pre", "table", "tr", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
}
_DROP_TAGS = {"script", "style", "iframe", "noscript", "svg", "form", "video", "audio"}


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{sha1(url)}.json"


def _inline_markdown(node: Tag) -> str:
    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
        elif isinstance(child, Tag):
            if child.name in _DROP_TAGS:
                continue
            if child.name in ("strong", "b"):
                out.append(f"**{_inline_markdown(child).strip()}**")
            elif child.name in ("em", "i"):
                out.append(f"*{_inline_markdown(child).strip()}*")
            elif child.name == "code":
                out.append(f"`{child.get_text(strip=True)}`")
            elif child.name == "a":
                href = child.get("href", "")
                label = _inline_markdown(child).strip() or href
                out.append(f"[{label}]({href})" if href else label)
            elif child.name == "br":
                out.append("\n")
            else:
                out.append(_inline_markdown(child))
    return "".join(out)


def html_to_markdown(root: Tag) -> str:
    """Convert an itchy description node to compact Markdown (no HTML tags left)."""
    blocks: list[str] = []
    for node in root.children:
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                blocks.append(text)
            continue
        if not isinstance(node, Tag) or node.name in _DROP_TAGS:
            continue
        if node.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(node.name[1])
            blocks.append(f"{'#' * min(level + 1, 6)} {node.get_text(' ', strip=True)}")
        elif node.name == "li":
            blocks.append(f"- {_inline_markdown(node).strip()}")
        elif node.name in ("ul", "ol"):
            for li in node.find_all("li", recursive=False):
                blocks.append(f"- {_inline_markdown(li).strip()}")
        elif node.name == "pre":
            blocks.append(f"```\n{node.get_text()}\n```")
        else:
            text = _inline_markdown(node)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                blocks.append(text)
    result = "\n\n".join(blocks)
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _text(el: Tag | None) -> str | None:
    if el is None:
        return None
    value = el.get_text(" ", strip=True)
    return value or None


def parse_page(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = _text(soup.select_one("h1.game_title"))
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = (og.get("content") if og else None) or (_text(soup.select_one("title")))
        if title:
            title = re.sub(r"\s+by\s+.*$", "", title).strip()

    author_el = soup.select_one("a.user_title") or soup.select_one(".game_author a")
    author = _text(author_el)
    author_url = author_el.get("href") if author_el and author_el.has_attr("href") else None

    cover_el = soup.select_one('meta[property="og:image"]')
    cover_url = cover_el.get("content") if cover_el else None

    tags: list[str] = []
    for tag_el in soup.select("div.game_tags a.tag, .game_tags a, a.tag"):
        value = tag_el.get_text(" ", strip=True)
        if value and value.lower() not in {t.lower() for t in tags}:
            tags.append(value)

    desc_el = (
        soup.select_one("div.formatted_description")
        or soup.select_one(".formatted_description")
        or soup.select_one("#game_description")
    )
    description = html_to_markdown(desc_el) if desc_el else ""
    if not description:
        og_desc = soup.select_one('meta[name="description"]')
        description = (og_desc.get("content") if og_desc else "") or ""

    # Free-form page text, used for license detection and author checks.
    for junk in soup.select("script, style, nav, header, footer"):
        junk.decompose()
    page_text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))

    return {
        "url": url,
        "title": title,
        "author": author,
        "author_url": author_url,
        "cover_url": cover_url,
        "tags": tags,
        "description": description,
        "page_text": page_text,
    }


def fetch_page(url: str, cache_dir: Path, *, force: bool = False, timeout: float = 45.0) -> dict[str, Any] | None:
    """Fetch a page with ETag caching. Returns parsed data or None on failure."""
    from .util import http_fetch

    cache_file = _cache_path(cache_dir, url)
    cached = None if force else read_json(cache_file)
    headers: dict[str, str] = {"Accept": "text/html,application/xhtml+xml"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    try:
        status, resp_headers, body = http_fetch(url, headers=headers, timeout=timeout)
    except HttpError as exc:
        if exc.status == 404:
            info(f"  page not found (404): {url}")
            return None
        if exc.status in (401, 403):
            info(f"  page is not public (HTTP {exc.status}): {url}")
            return None
        info(f"  failed to fetch {url}: {exc}")
        return None
    except Exception as exc:  # network flake should not kill the run
        info(f"  failed to fetch {url}: {exc}")
        return None

    if status == 304 and cached:
        return cached.get("parsed")

    html = body.decode("utf-8", "replace")
    parsed = parse_page(html, url)
    write_json(
        cache_file,
        {
            "url": url,
            "etag": resp_headers.get("etag"),
            "last_modified": resp_headers.get("last-modified"),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parsed": parsed,
        },
    )
    return parsed
