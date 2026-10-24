"""Small shared helpers: slugs, bytes formatting, HTTP with retries, JSON IO."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "ZAC/1.0 (Zhura's Asset Classifier; local asset ledger; +https://itch.io)"

_VERSION_SUFFIX = re.compile(r"[_\- ]?v\d+(?:[._]\d+)*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Filesystem/URL friendly slug, deterministic and collision-safe enough."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = _VERSION_SUFFIX.sub("", text)
    text = _NON_ALNUM.sub("-", text).strip("-")
    return text or "unnamed"


def normalize_name(text: str) -> str:
    """Aggressive normalization used for fuzzy folder<->asset matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = _VERSION_SUFFIX.sub("", text)
    return _NON_ALNUM.sub("", text)


def slug_from_url(url: str) -> str | None:
    """https://author.itch.io/pack-name -> pack-name"""
    if not url:
        return None
    m = re.search(r"\.itch\.io/([^/?#]+)", url)
    return m.group(1) if m else None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def human_bytes(num: int | float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        warn(f"could not read {path}: {exc}")
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    tmp.replace(path)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def info(message: str) -> None:
    print(message, file=sys.stderr)


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: bytes):
        self.url = url
        self.status = status
        self.body = body
        snippet = body[:400].decode("utf-8", "replace").strip()
        super().__init__(f"HTTP {status} for {url}: {snippet}")


def http_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    retries: int = 3,
    timeout: float = 45.0,
    backoff: float = 2.0,
) -> tuple[int, dict[str, str], bytes]:
    """Fetch a URL. Returns (status, headers, body). 4xx is not retried."""
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        merged.update(headers)

    last_exc: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(backoff * attempt)
        req = urllib.request.Request(url, data=data, headers=merged, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if resp_headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                return resp.status, resp_headers, body
        except urllib.error.HTTPError as exc:
            body = exc.read() or b""
            if exc.headers and (exc.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise HttpError(url, exc.code, body) from exc
            last_exc = HttpError(url, exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
    raise last_exc if last_exc else RuntimeError(f"failed to fetch {url}")


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def relpath_str(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def env_api_key() -> str | None:
    key = os.environ.get("ITCH_API_KEY", "").strip()
    return key or None
