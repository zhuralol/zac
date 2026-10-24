"""File classification: extension -> category, junk filtering, archive detection."""

from __future__ import annotations

import os
from pathlib import PurePosixPath

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tga", ".webp", ".tif", ".tiff",
    ".ico", ".dds", ".exr", ".hdr", ".pbm", ".pgm", ".ppm",
}
VECTOR_EXTS = {".svg", ".ai", ".eps", ".pdf"}
MODEL3D_EXTS = {".obj", ".fbx", ".gltf", ".glb", ".blend", ".dae", ".stl", ".3ds", ".ply", ".ma", ".mb"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".aiff", ".aif", ".m4a", ".opus", ".wma"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2", ".fnt"}
TILEMAP_EXTS = {".tmx", ".tsx", ".tsj", ".ldtk", ".json", ".atlas", ".tps"}
SOURCE_EXTS = {".aseprite", ".ase", ".psd", ".kra", ".xcf", ".blend", ".clip", ".sai", ".procreate"}
SHADER_EXTS = {".glsl", ".shader", ".hlsl", ".frag", ".vert", ".wgsl", ".metal"}
DOC_EXTS = {".txt", ".md", ".pdf", ".rtf", ".doc", ".docx", ".html", ".htm", ".json"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
AUDIO_EXTS_FOR_PROBE = AUDIO_EXTS
PROBE_IMAGE_EXTS = IMAGE_EXTS
PROBE_FONT_EXTS = FONT_EXTS

JUNK_DIRS = {"__MACOSX", ".git", ".svn", ".idea", ".vscode", "node_modules", ".DS_Store"}
JUNK_FILES = {"thumbs.db", "desktop.ini", ".ds_store", ".gitignore", "icon\r"}
JUNK_SUFFIXES = (".url", ".webloc", ".lnk")

CATEGORY_LABELS = {
    "images": "sprites/textures",
    "vector": "vector art",
    "models_3d": "3D models",
    "audio": "audio",
    "fonts": "fonts",
    "tilemaps": "tilemaps/atlas metadata",
    "source_projects": "source projects",
    "shaders": "shaders",
    "docs": "docs/licenses",
    "archives": "nested archives",
    "other": "other",
}

# Extension priority: a .json could be tilemap data; .aseprite is a source project.
_SPECIFIC = {
    **{e: "source_projects" for e in SOURCE_EXTS},
    **{e: "vector" for e in VECTOR_EXTS},
    **{e: "images" for e in IMAGE_EXTS},
    **{e: "models_3d" for e in MODEL3D_EXTS},
    **{e: "audio" for e in AUDIO_EXTS},
    **{e: "fonts" for e in FONT_EXTS},
    **{e: "shaders" for e in SHADER_EXTS},
    **{e: "tilemaps" for e in TILEMAP_EXTS},
    **{e: "docs" for e in DOC_EXTS},
    **{e: "archives" for e in ARCHIVE_EXTS},
}


def ext_of(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def is_junk_file(name: str) -> bool:
    low = name.lower()
    if low in JUNK_FILES:
        return True
    return low.endswith(JUNK_SUFFIXES)


def is_junk_dir(name: str) -> bool:
    return name.lower() in {d.lower() for d in JUNK_DIRS}


def classify(name: str) -> str:
    return _SPECIFIC.get(ext_of(name), "other")


def is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(e) for e in (".zip", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))


def is_probable_license_doc(name: str) -> bool:
    stem = PurePosixPath(name.replace("\\", "/")).stem.lower()
    return any(tok in stem for tok in ("license", "licence", "copying", "terms", "eula", "readme", "credits"))


def category_label(cat: str) -> str:
    return CATEGORY_LABELS.get(cat, cat)


def iter_files(folder: str):
    """Yield absolute paths and relative posix paths, skipping junk."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not is_junk_dir(d))
        for name in sorted(files):
            if is_junk_file(name):
                continue
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, folder).replace(os.sep, "/")
            yield abs_path, rel
