"""Turning metadata into a path on the SSD: year / month / place / file."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from .config import Config
from .util import month_folder, sanitize_component, sanitize_filename

NO_DATE_FOLDER = "fara_data"


def place_folder(place: str | None, cfg: Config) -> str:
    if not place:
        return sanitize_component(cfg.unknown_place, fallback="Fara_locatie")
    return sanitize_component(place, fallback=cfg.unknown_place)


def target_filename(name: str, taken_at: datetime | None, cfg: Config,
                    stem_override: str | None = None) -> str:
    """The final filename. By default we keep the name the camera gave it."""
    clean = sanitize_filename(name)
    if stem_override:
        ext = clean[clean.rfind("."):] if "." in clean else ""
        return f"{stem_override}{ext}"
    if cfg.rename_with_timestamp and taken_at is not None:
        prefix = taken_at.strftime("%Y%m%d_%H%M%S")
        if clean.startswith(prefix):
            return clean
        return f"{prefix}_{clean}"
    return clean


def dest_dir(taken_at: datetime | None, place: str | None, kind: str,
             cfg: Config) -> PurePosixPath:
    """The folder, relative to the backup root, where a file belongs."""
    if taken_at is None:
        parts = [NO_DATE_FOLDER, place_folder(place, cfg)]
    else:
        parts = [
            f"{taken_at.year:04d}",
            month_folder(taken_at.month, cfg.language),
            place_folder(place, cfg),
        ]
    if kind == "video" and cfg.video_subdir:
        parts.append(sanitize_component(cfg.video_subdir, fallback="Video"))
    return PurePosixPath(*parts)


def dest_relpath(name: str, taken_at: datetime | None, place: str | None,
                 kind: str, cfg: Config, stem_override: str | None = None) -> PurePosixPath:
    return dest_dir(taken_at, place, kind, cfg) / target_filename(
        name, taken_at, cfg, stem_override
    )
