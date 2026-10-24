"""Deep file probing: image metadata + pixel-art heuristics, audio duration,
3D mesh counts, fonts, and plain-text extraction for license detection.

Every probe is best-effort: a failure yields `{"kind": "unprobed"}` and never
aborts a scan.
"""

from __future__ import annotations

import io
import json
from typing import Any

from .taxonomy import (
    PROBE_FONT_EXTS,
    PROBE_IMAGE_EXTS,
    classify,
    ext_of,
)

MAX_PROBE_BYTES = 32 * 1024 * 1024  # don't load huge files into memory
MAX_DOC_CHARS = 20_000


def _probe_image(data: bytes) -> dict[str, Any]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
        frames = int(getattr(im, "n_frames", 1) or 1)
        colors = im.convert("RGB").getcolors(maxcolors=257)
        palette = len(colors) if colors is not None else 257
        if palette > 256:
            palette_bucket = ">256"
        elif palette <= 16:
            palette_bucket = "<=16"
        elif palette <= 32:
            palette_bucket = "<=32"
        elif palette <= 64:
            palette_bucket = "<=64"
        elif palette <= 128:
            palette_bucket = "<=128"
        else:
            palette_bucket = "<=256"

    longest = max(width, height)
    if longest <= 16:
        size_bucket = "<=16"
    elif longest <= 32:
        size_bucket = "17-32"
    elif longest <= 64:
        size_bucket = "33-64"
    elif longest <= 128:
        size_bucket = "65-128"
    elif longest <= 512:
        size_bucket = "129-512"
    else:
        size_bucket = ">512"

    pixel_art = palette <= 64 and longest <= 512 and not (width >= 256 and height >= 256 and palette > 64)

    return {
        "kind": "image",
        "width": width,
        "height": height,
        "frames": frames,
        "palette": palette if palette <= 256 else None,
        "palette_bucket": palette_bucket,
        "size_bucket": size_bucket,
        "pixel_art": bool(pixel_art),
    }


def _probe_audio(data: bytes) -> dict[str, Any]:
    import mutagen

    audio = mutagen.File(io.BytesIO(data))
    if audio is None or getattr(audio, "info", None) is None:
        return {"kind": "unprobed"}
    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    if duration < 3:
        role = "sfx"
    elif duration > 30:
        role = "music"
    else:
        role = "short-music-or-voice"
    return {
        "kind": "audio",
        "duration": round(duration, 2),
        "sample_rate": getattr(audio.info, "sample_rate", None),
        "channels": getattr(audio.info, "channels", None),
        "role": role,
    }


def _probe_gltf(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {"kind": "model3d"}
    return {
        "kind": "model3d",
        "meshes": len(doc.get("meshes") or []),
        "nodes": len(doc.get("nodes") or []),
        "materials": len(doc.get("materials") or []),
        "animations": len(doc.get("animations") or []),
    }


def _probe_obj(text: str) -> dict[str, Any]:
    verts = faces = objects = 0
    for line in text.splitlines():
        if line.startswith("v "):
            verts += 1
        elif line.startswith("f "):
            faces += 1
        elif line.startswith("o ") or line.startswith("g "):
            objects += 1
    return {"kind": "model3d", "vertices": verts, "faces": faces, "objects": objects}


def _probe_font(data: bytes) -> dict[str, Any]:
    from fontTools.ttLib import TTFont

    with TTFont(io.BytesIO(data), fontNumber=0, lazy=True) as font:
        glyphs = font["maxp"].numGlyphs if "maxp" in font else None
        name = None
        try:
            family = font["name"].getDebugName(1)
            style = font["name"].getDebugName(2)
            name = " ".join(x for x in (family, style) if x) or None
        except Exception:
            pass
    return {"kind": "font", "glyphs": glyphs, "name": name}


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def probe_bytes(name: str, data: bytes) -> dict[str, Any]:
    """Probe one file's bytes. Returns a facts dict (always has `kind`)."""
    ext = ext_of(name)
    category = classify(name)
    try:
        if ext in PROBE_IMAGE_EXTS:
            return _probe_image(data)
        if category == "audio" or ext in (".wav", ".ogg", ".mp3", ".flac"):
            return _probe_audio(data)
        if ext == ".gltf":
            return _probe_gltf(_decode_text(data))
        if ext == ".obj":
            return _probe_obj(_decode_text(data))
        if ext in PROBE_FONT_EXTS:
            return _probe_font(data)
        if category == "docs" or ext in (".txt", ".md", ".license"):
            return {"kind": "doc", "text": _decode_text(data)[:MAX_DOC_CHARS]}
        if ext in (".glsl", ".shader", ".hlsl", ".frag", ".vert", ".wgsl"):
            return {"kind": "shader"}
    except ImportError as exc:
        return {"kind": "unprobed", "reason": f"missing optional dependency: {exc.name}"}
    except Exception as exc:  # corrupt files must not stop the scan
        return {"kind": "unprobed", "reason": str(exc)[:200]}
    return {"kind": "other"}


def probe_path(abs_path: str, rel_path: str, *, size: int, mtime: float, cache: dict[str, Any]) -> dict[str, Any]:
    """Probe a real file, using/populating the fingerprint cache."""
    from .util import sha1

    key = sha1(f"{rel_path}|{size}|{int(mtime)}")
    cached = cache.get(key)
    if cached is not None:
        return cached
    if size > MAX_PROBE_BYTES:
        facts = {"kind": "skipped", "reason": f"file too large ({size} bytes)"}
    else:
        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
            facts = probe_bytes(rel_path, data)
        except OSError as exc:
            facts = {"kind": "unprobed", "reason": str(exc)[:200]}
    cache[key] = facts
    return facts
