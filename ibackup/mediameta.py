"""Extracting the metadata the sorting depends on: when, and where.

The order of trust for the date is always the same: what is inside the file,
then the filename, then the modification time. The label of whichever source
won is stored in the index, so we can tell later how confident a photo's
placement really is.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import exif, isobmff

PHOTO_EXT = {
    ".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".png", ".tif", ".tiff",
    ".dng", ".gif", ".bmp", ".webp", ".avif", ".cr2", ".cr3", ".nef",
    ".arw", ".raf", ".orf", ".rw2", ".srw", ".pef",
}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".3g2", ".mkv", ".mpg", ".mpeg"}
SIDECAR_EXT = {".aae", ".xmp"}
ALL_EXT = PHOTO_EXT | VIDEO_EXT | SIDECAR_EXT

# How much of a large JPEG we read while looking for its EXIF block.
JPEG_HEAD = 512 * 1024


@dataclass
class MediaMeta:
    taken_at: datetime | None = None          # local time, naive
    taken_at_source: str = ""
    lat: float | None = None
    lon: float | None = None
    gps_source: str = ""
    kind: str = "other"       # photo | screenshot | video | sidecar | other
    make: str | None = None
    model: str | None = None
    software: str | None = None
    screenshot_reason: str = ""

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None


def kind_for_name(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return "other"


def is_media_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in ALL_EXT


# --- the date hidden in the filename ---------------------------------------

_NAME_DATE = re.compile(
    r"(?<!\d)(19[7-9]\d|20[0-4]\d)[-_.:]?(0[1-9]|1[0-2])[-_.:]?(0[1-9]|[12]\d|3[01])"
    # The separator between date and time: one character, or the " at " / " la "
    # used by screenshot names ("Screenshot 2023-05-01 at 12.33.44").
    r"(?:(?:[-_.:T ]|\s(?:at|la)\s)?([01]\d|2[0-3])[-_.:]?([0-5]\d)"
    r"(?:[-_.:]?([0-5]\d))?)?(?!\d)"
)
_EPOCH_MS = re.compile(r"(?<!\d)(1[0-9]{9})(?:[0-9]{3})?(?!\d)")


def datetime_from_name(name: str):
    """Reads IMG_20190712_183042, 2019-07-12 18.30.42, IMG-20190712-WA0001."""
    match = _NAME_DATE.search(name)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        second = int(match.group(6) or 0)
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    match = _EPOCH_MS.search(name)
    if match:
        try:
            stamp = int(match.group(1))
            if 946684800 <= stamp <= 2145916800:  # 2000..2038
                return datetime.fromtimestamp(stamp)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _to_local_naive(dt: datetime | None, assume_utc: bool = False):
    """Everything becomes local wall-clock time, which is what folders want."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    if assume_utc:
        return dt.astimezone().replace(tzinfo=None)
    # The offset stored in the file already described local time on location.
    return dt.replace(tzinfo=None)


# --- extraction -------------------------------------------------------------

def _read_exif_block(path: Path, ext: str):
    """The raw TIFF/EXIF block, whatever the container."""
    with path.open("rb") as fh:
        head = fh.read(16)
        fh.seek(0)
        if ext in (".jpg", ".jpeg", ".jpe") or head[:2] == b"\xff\xd8":
            return exif.exif_from_jpeg(fh.read(JPEG_HEAD))
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return exif.exif_from_png(fh.read(JPEG_HEAD))
        if head[:2] in (b"II", b"MM"):  # TIFF, DNG and most RAW formats
            return fh.read(JPEG_HEAD)
        size = path.stat().st_size
        if isobmff.container_kind(fh, size) == "heif":
            return isobmff.heif_exif_block(fh, size)
    return None


def extract(path, *, logical_path=None, use_filename: bool = True,
            use_mtime: bool = True, detect_screenshots: bool = True) -> MediaMeta:
    """Metadata for a local file. Never raises on a damaged file.

    `logical_path` is where the file sits inside its source; the name and
    folder rules read it instead of `path`, which may be a staging copy.
    """
    path = Path(path)
    logical = Path(logical_path) if logical_path is not None else path
    name = logical.name
    ext = path.suffix.lower()
    meta = MediaMeta(kind=kind_for_name(name))

    if meta.kind == "video":
        try:
            with path.open("rb") as fh:
                size = path.stat().st_size
                info = isobmff.quicktime_metadata(fh, size)
            assume_utc = info["taken_at_source"] == "qt:mvhd"
            meta.taken_at = _to_local_naive(info["taken_at"], assume_utc=assume_utc)
            meta.taken_at_source = info["taken_at_source"]
            if info["gps"]:
                meta.lat, meta.lon = info["gps"]
                meta.gps_source = info["gps_source"]
            meta.make, meta.model = info.get("make"), info.get("model")
        except Exception:
            pass
    elif meta.kind == "photo":
        try:
            block = _read_exif_block(path, ext)
            if block:
                tags = exif.parse_tiff(block)
                meta.taken_at, meta.taken_at_source = exif.exif_datetime(tags)
                coords = exif.exif_gps(tags)
                if coords:
                    meta.lat, meta.lon = coords
                    meta.gps_source = "exif:GPS"
                meta.make, meta.model = exif.exif_camera(tags)
                meta.software = exif.exif_software(tags)
                if meta.taken_at is None:
                    gps_dt = exif.exif_gps_datetime(tags)
                    if gps_dt is not None:
                        meta.taken_at = _to_local_naive(gps_dt, assume_utc=True)
                        meta.taken_at_source = "exif:GPSDateStamp"
        except Exception:
            pass

    # Runs before the filename and mtime fallbacks, while taken_at_source still
    # tells us truthfully whether the file carried an EXIF date of its own.
    if meta.kind == "photo" and detect_screenshots:
        from .screenshot import screenshot_reason
        reason = screenshot_reason(logical, meta)
        if reason is not None:
            meta.kind = "screenshot"
            meta.screenshot_reason = reason

    if meta.taken_at is None and use_filename:
        from_name = datetime_from_name(name)
        if from_name is not None:
            meta.taken_at, meta.taken_at_source = from_name, "filename"

    if meta.taken_at is None and use_mtime:
        try:
            stamp = path.stat().st_mtime
            meta.taken_at = datetime.fromtimestamp(stamp)
            meta.taken_at_source = "mtime"
        except (OSError, OverflowError, ValueError):
            pass

    return meta


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
