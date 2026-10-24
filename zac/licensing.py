"""License detection and attribution line generation.

Priority: LICENSE/README files inside the pack > itch page description/page text
> unspecified. Matched raw spans are preserved so a human can verify the guess.
"""

from __future__ import annotations

import re
from typing import Any

# (regex, canonical name, requires_attribution)  — ordered most-specific first.
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bcc[ -]?0\b|creative\s+commons\s+zero|public\s+domain", re.I), "CC0 / Public Domain", "no"),
    (re.compile(r"\bcc[ -]?by[ -]?nc[ -]?sa\b", re.I), "CC BY-NC-SA", "yes"),
    (re.compile(r"\bcc[ -]?by[ -]?nc[ -]?nd\b", re.I), "CC BY-NC-ND", "yes"),
    (re.compile(r"\bcc[ -]?by[ -]?nc\b", re.I), "CC BY-NC", "yes"),
    (re.compile(r"\bcc[ -]?by[ -]?sa\b", re.I), "CC BY-SA", "yes"),
    (re.compile(r"\bcc[ -]?by[ -]?nd\b", re.I), "CC BY-ND", "yes"),
    (re.compile(r"\bcc[ -]?by\b|creative\s+commons\s+attribution", re.I), "CC BY", "yes"),
    (re.compile(r"\bapache[ -]?(?:license[ ,]*)?2\.0\b", re.I), "Apache-2.0", "yes"),
    (re.compile(r"\blgpl\b", re.I), "LGPL", "yes"),
    (re.compile(r"\bgpl[ -]?v?3\b|\bgnu\s+general\s+public\s+license[^.\n]{0,20}3", re.I), "GPL-3.0", "yes"),
    (re.compile(r"\bgpl[ -]?v?2\b|\bgnu\s+general\s+public\s+license", re.I), "GPL", "yes"),
    (re.compile(r"\bmpl[ -]?2\.0\b|mozilla\s+public\s+license", re.I), "MPL-2.0", "yes"),
    (re.compile(r"\bmit\s+licen[cs]e\b|\bmit\b(?=[^\n]{0,40}licen[cs]e)", re.I), "MIT", "yes"),
    (re.compile(r"\bogl\b|open\s+game\s+license", re.I), "OGL", "yes"),
    (re.compile(r"royalty[ -]?free", re.I), "Royalty-free (custom terms)", "unknown"),
    (re.compile(r"no\s+attribution\s+(?:is\s+)?(?:required|necessary)", re.I), "Custom (no attribution required)", "no"),
    (re.compile(r"(?:credits?|attribution)\s+(?:are|is)?\s*not\s+(?:mandatory|required)", re.I),
     "Custom (no attribution required)", "no"),
    (re.compile(r"credit\s+is\s+not\s+required|no\s+credit\s+required", re.I), "Custom (no attribution required)", "no"),
    (re.compile(r"attribution\s+(?:is\s+)?required|with\s+attribution|proper\s+credit|please\s+credit", re.I),
     "Custom (attribution required)", "yes"),
    (re.compile(r"free\s+for\s+(?:personal\s+and\s+)?commercial\s+use|commercial\s+use\s+(?:is\s+)?(?:allowed|permitted)|"
                r"even\s+commercial", re.I),
     "Custom (commercial use permitted)", "unknown"),
]

_CC_URL = re.compile(
    r"creativecommons\.org/(?:"
    r"(?P<zero>publicdomain/zero)"
    r"|licenses/(?P<kind>by(?:-[a-z]{2})*)/(?P<version>\d+\.\d+)"
    r")",
    re.I,
)

_UNSPECIFIED = {
    "type": "unspecified",
    "confidence": "none",
    "source": None,
    "raw_span": None,
    "requires_attribution": "unknown",
}

_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _span(text: str, match: re.Match[str], width: int = 160) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def detect_license(text: str) -> dict[str, Any] | None:
    """Return the strongest license match in `text`, or None."""
    if not text:
        return None

    # Creative Commons URLs are the most specific signal we can get.
    url_match = _CC_URL.search(text)
    if url_match:
        if url_match.group("zero"):
            cc_type, requires = "CC0 / Public Domain", "no"
        else:
            kind = url_match.group("kind").lower()
            cc_type = "CC " + kind.upper() + " " + url_match.group("version")
            requires = "yes"
        return {
            "type": cc_type,
            "confidence": "medium",
            "source": "page",
            "raw_span": _span(text, url_match),
            "requires_attribution": requires,
        }

    best: tuple[int, dict[str, Any]] | None = None
    for order, (pattern, name, requires) in enumerate(_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        canonical = name
        if name == "CC BY":
            version = _VERSION.search(text[match.end(): match.end() + 40])
            canonical = f"CC BY {version.group(1)}" if version else "CC BY"
        candidate = {
            "type": canonical,
            "confidence": "medium",
            "source": "page",
            "raw_span": _span(text, match),
            "requires_attribution": requires,
        }
        if best is None or order < best[0]:
            best = (order, candidate)
        if order == 0:
            break  # CC0/public domain wins outright
    return best[1] if best else None


def detect_from_docs(docs: list[dict[str, str]]) -> dict[str, Any] | None:
    """docs: [{"name": ..., "text": ...}] from the downloaded pack (highest confidence)."""
    for doc in docs:
        found = detect_license(doc.get("text", ""))
        if found:
            found = dict(found)
            found["confidence"] = "high"
            found["source"] = doc.get("name")
            return found
    license_named = [d for d in docs if re.search(r"licen[cs]e|eula|terms", d.get("name", ""), re.I)]
    if license_named:
        doc = license_named[0]
        return {
            "type": f"custom (see {doc.get('name')})",
            "confidence": "medium",
            "source": doc.get("name"),
            "raw_span": doc.get("text", "")[:200].strip() or None,
            "requires_attribution": "unknown",
        }
    return None


def credit_line(itch: dict[str, Any], license_type: str, requires: str) -> str:
    title = itch.get("title") or "Untitled asset"
    author = itch.get("author") or "unknown author"
    url = itch.get("author_url") or itch.get("url") or ""
    parts = f"{title} by {author}"
    if url:
        parts += f" ({url})"
    if license_type and license_type != "unspecified":
        parts += f" — {license_type}"
    if requires == "no":
        parts += " [attribution not required; credit optional]"
    elif requires == "unknown":
        parts += " [license requires manual verification]"
    return parts


def build_attribution(
    itch: dict[str, Any],
    description: str = "",
    page_text: str = "",
    docs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    docs = docs or []
    license_info = detect_from_docs(docs)
    if license_info is None:
        license_info = detect_license(description) or detect_license(page_text)
    if license_info is None:
        license_info = dict(_UNSPECIFIED)

    requires = license_info.get("requires_attribution", "unknown")
    return {
        "author": itch.get("author"),
        "author_url": itch.get("author_url"),
        "published_at": itch.get("published_at"),
        "license": {
            "type": license_info.get("type"),
            "confidence": license_info.get("confidence"),
            "source": license_info.get("source"),
            "raw_span": license_info.get("raw_span"),
        },
        "requires_attribution": requires,
        "credit_line": credit_line(itch, license_info.get("type") or "unspecified", requires),
    }
