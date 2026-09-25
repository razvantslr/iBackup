"""Extragerea metadatelor de care depinde sortarea: cand si unde.

Ordinea de incredere pentru data este intotdeauna aceeasi: metadatele din
fisier, apoi numele fisierului, apoi data de modificare. Salvam si eticheta
sursei in baza de date, ca sa stim mai tarziu cat de sigura e sortarea unei poze.
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

# Cat citim din cap/coada cand cautam EXIF intr-un JPEG mare.
JPEG_HEAD = 512 * 1024


@dataclass
class MediaMeta:
    taken_at: datetime | None = None          # ora locala, naiva
    taken_at_source: str = ""
    lat: float | None = None
    lon: float | None = None
    gps_source: str = ""
    kind: str = "other"                       # photo | video | sidecar | other
    make: str | None = None
    model: str | None = None

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


# --- data din numele fisierului --------------------------------------------

_NAME_DATE = re.compile(
    r"(?<!\d)(19[7-9]\d|20[0-4]\d)[-_.:]?(0[1-9]|1[0-2])[-_.:]?(0[1-9]|[12]\d|3[01])"
    # Separatorul dintre data si ora: un caracter, sau " at " / " la " din
    # numele de screenshot-uri ("Screenshot 2023-05-01 at 12.33.44").
    r"(?:(?:[-_.:T ]|\s(?:at|la)\s)?([01]\d|2[0-3])[-_.:]?([0-5]\d)"
    r"(?:[-_.:]?([0-5]\d))?)?(?!\d)"
)
_EPOCH_MS = re.compile(r"(?<!\d)(1[0-9]{9})(?:[0-9]{3})?(?!\d)")


def datetime_from_name(name: str):
    """Scoate data din nume: IMG_20190712_183042, 2019-07-12 18.30.42, IMG-20190712-WA0001."""
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
    """Aducem totul la ora locala de perete, fara fus, cum o vrem in foldere."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    if assume_utc:
        return dt.astimezone().replace(tzinfo=None)
    # Offset-ul scris in fisier era deja ora locala de la locul filmarii.
    return dt.replace(tzinfo=None)


# --- extragerea propriu-zisa -----------------------------------------------

def _read_exif_block(path: Path, ext: str):
    """Blocul TIFF/EXIF brut, indiferent de container."""
    with path.open("rb") as fh:
        head = fh.read(16)
        fh.seek(0)
        if ext in (".jpg", ".jpeg", ".jpe") or head[:2] == b"\xff\xd8":
            return exif.exif_from_jpeg(fh.read(JPEG_HEAD))
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return exif.exif_from_png(fh.read(JPEG_HEAD))
        if head[:2] in (b"II", b"MM"):  # TIFF, DNG si majoritatea RAW-urilor
            return fh.read(JPEG_HEAD)
        size = path.stat().st_size
        if isobmff.container_kind(fh, size) == "heif":
            return isobmff.heif_exif_block(fh, size)
    return None


def extract(path, *, use_filename: bool = True, use_mtime: bool = True) -> MediaMeta:
    """Metadatele unui fisier local. Nu arunca niciodata pe fisiere stricate."""
    path = Path(path)
    name = path.name
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
                if meta.taken_at is None:
                    gps_dt = exif.exif_gps_datetime(tags)
                    if gps_dt is not None:
                        meta.taken_at = _to_local_naive(gps_dt, assume_utc=True)
                        meta.taken_at_source = "exif:GPSDateStamp"
        except Exception:
            pass

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
