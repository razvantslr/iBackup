"""Turning what the user typed on the command line into a Source."""
from __future__ import annotations

from pathlib import Path

from .base import Localized, Source, SourceItem
from .folder import FolderSource

__all__ = ["Source", "SourceItem", "Localized", "FolderSource", "build_source",
           "describe_backends"]


def build_source(spec: str, cfg) -> Source:
    """spec: a folder, 'iphone', 'iphone:afc', 'iphone:mtp' or 'folder:<path>'."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("You did not say where to copy from. Use --source.")

    lowered = spec.lower()
    if lowered.startswith("folder:"):
        return _folder(spec.split(":", 1)[1], cfg)
    if lowered in ("iphone", "iphone:auto", "phone", "auto"):
        return _iphone(None, cfg)
    if lowered.startswith("iphone:"):
        return _iphone(lowered.split(":", 1)[1], cfg)
    return _folder(spec, cfg)


def _folder(path: str, cfg) -> FolderSource:
    root = Path(path.strip('"')).expanduser()
    _refuse_overlap(root, Path(cfg.dest))
    return FolderSource(
        root,
        skip_dirs=cfg.skip_dirs,
        include_videos=cfg.include_videos,
        include_sidecars=cfg.include_sidecars,
    )


def _refuse_overlap(source_root: Path, dest_root: Path) -> None:
    """The source and the backup must not contain one another.

    Otherwise we would copy the backup into itself: on the first run, with an
    empty index, every already-sorted file would look new and be written again
    somewhere else.
    """
    try:
        source_abs = source_root.resolve()
        dest_abs = dest_root.resolve()
    except OSError:
        return
    if source_abs == dest_abs:
        raise ValueError(
            f"The source and the destination are the same folder ({source_abs}). "
            "To rebuild the index from what is already there, use the 'reindex' "
            "command."
        )
    if dest_abs in source_abs.parents:
        raise ValueError(
            f"The source {source_abs} sits inside the backup {dest_abs}. "
            "Pick a source outside the backup."
        )
    if source_abs in dest_abs.parents:
        raise ValueError(
            f"The backup {dest_abs} sits inside the source {source_abs}, so the "
            "scan would include files that are already copied. Pick a subfolder "
            "of the source, or move the backup out of it."
        )


def _iphone(backend: str | None, cfg) -> Source:
    """AFC is the first choice; MTP is the fallback when Apple's driver is absent."""
    errors = []
    order = [backend] if backend else ["afc", "mtp"]
    for name in order:
        if name == "afc":
            from .afc import AfcSource, AfcUnavailable
            try:
                return AfcSource(include_videos=cfg.include_videos,
                                 include_sidecars=cfg.include_sidecars)
            except AfcUnavailable as exc:
                errors.append(f"AFC: {exc}")
            except Exception as exc:
                errors.append(f"AFC: {type(exc).__name__}: {exc}")
        elif name == "mtp":
            from .mtp import MtpSource, MtpUnavailable
            try:
                return MtpSource(include_videos=cfg.include_videos,
                                 include_sidecars=cfg.include_sidecars)
            except MtpUnavailable as exc:
                errors.append(f"MTP: {exc}")
            except Exception as exc:
                errors.append(f"MTP: {type(exc).__name__}: {exc}")
        else:
            raise ValueError(f"Unknown iPhone backend: {name}")
    raise RuntimeError("Could not open the iPhone.\n  " + "\n  ".join(errors))


def describe_backends() -> list[dict]:
    """Which ways of reaching the phone are available on this machine right now."""
    out = []
    try:
        from .afc import list_devices as afc_devices
        devices = afc_devices()
        out.append({"backend": "afc", "available": True,
                    "detail": f"{len(devices)} device(s) on USB", "devices": devices})
    except Exception as exc:
        out.append({"backend": "afc", "available": False, "detail": str(exc),
                    "devices": []})
    try:
        from .mtp import list_devices as mtp_devices
        names = mtp_devices()
        out.append({"backend": "mtp", "available": True,
                    "detail": f"{len(names)} portable device(s)", "devices": names})
    except Exception as exc:
        out.append({"backend": "mtp", "available": False, "detail": str(exc),
                    "devices": []})
    return out
