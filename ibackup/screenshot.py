"""Telling screenshots apart from photographs.

Screenshots are reference material - a receipt, a recipe, a chat - not memories,
and on a phone they are a large share of the camera roll. Keeping them in their
own tree stops them from drowning the photos.

Ported from the v0.1 scanner, with two changes. It no longer needs Pillow: the
EXIF we already parse gives us the Software tag and the camera name. And the
resolution heuristic is gone - v0.1 flagged anything at or below 1792x828, which
misfiled every resized or messaging-app photo. What replaces it is narrower: a
PNG with no EXIF date, no GPS and no camera is a screenshot, because a real
photograph carries at least one of those.
"""
from __future__ import annotations

import re
from pathlib import Path

# Compared against names with separators stripped, so "Screen Shot",
# "screen-shot" and "Screenshot" all collapse to the same token.
KEYWORDS = (
    "screenshot",
    "screencapture",
    "capturadeecran",
    "capturaecran",
)

_SEPARATORS = re.compile(r"[\s_\-.()\[\]]+")

PNG_EXT = ".png"


def _normalize(text: str) -> str:
    return _SEPARATORS.sub("", text.lower())


def _has_keyword(text: str) -> bool:
    flat = _normalize(text)
    return any(word in flat for word in KEYWORDS)


def screenshot_reason(path, meta) -> str | None:
    """Why this file looks like a screenshot, or None if it does not.

    Returning the reason rather than a bare boolean makes the rules reviewable:
    `import --verbose` prints it, so a misfiled photo is easy to trace back to
    the rule that caught it.
    """
    path = Path(path)

    if _has_keyword(path.name):
        return "filename"

    for part in path.parent.parts:
        if _has_keyword(part):
            return "folder name"

    software = (meta.software or "").lower()
    if "screenshot" in software or "screen capture" in software:
        return "EXIF Software"

    # A photograph taken by any camera carries at least a date, a camera name or
    # a location. A PNG with none of the three is something the screen produced.
    if path.suffix.lower() == PNG_EXT:
        if (not meta.taken_at_source.startswith("exif")
                and not meta.has_gps
                and not meta.make
                and not meta.model):
            return "PNG without camera metadata"

    return None


def is_screenshot(path, meta) -> bool:
    return screenshot_reason(path, meta) is not None
