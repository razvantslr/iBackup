"""Alegerea sursei din ce a scris utilizatorul in linia de comanda."""
from __future__ import annotations

from pathlib import Path

from .base import Localized, Source, SourceItem
from .folder import FolderSource

__all__ = ["Source", "SourceItem", "Localized", "FolderSource", "build_source",
           "describe_backends"]


def build_source(spec: str, cfg) -> Source:
    """spec: un folder, 'iphone', 'iphone:afc', 'iphone:mtp' sau 'folder:<cale>'."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("Nu ai spus de unde copiez. Foloseste --source.")

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
    """Sursa si backup-ul nu au voie sa se contina reciproc.

    Altfel am copia backup-ul in el insusi: la prima rulare, cu indexul gol,
    fiecare fisier deja sortat ar fi vazut ca poza noua si rescris in alta parte.
    """
    try:
        source_abs = source_root.resolve()
        dest_abs = dest_root.resolve()
    except OSError:
        return
    if source_abs == dest_abs:
        raise ValueError(
            f"Sursa si destinatia sunt acelasi folder ({source_abs}). "
            "Pentru a reconstrui indexul din ce exista deja, foloseste "
            "comanda 'reindex'."
        )
    if dest_abs in source_abs.parents:
        raise ValueError(
            f"Sursa {source_abs} e in interiorul backup-ului {dest_abs}. "
            "Alege o sursa din afara backup-ului."
        )
    if source_abs in dest_abs.parents:
        raise ValueError(
            f"Backup-ul {dest_abs} e in interiorul sursei {source_abs}, deci "
            "scanarea ar include si fisierele deja copiate. Alege un subfolder "
            "al sursei, sau muta backup-ul in afara ei."
        )


def _iphone(backend: str | None, cfg) -> Source:
    """AFC e prima optiune; MTP e plasa de siguranta cand lipseste driverul Apple."""
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
            raise ValueError(f"Backend necunoscut pentru iPhone: {name}")
    raise RuntimeError("Nu am putut deschide iPhone-ul.\n  " + "\n  ".join(errors))


def describe_backends() -> list[dict]:
    """Ce metode de acces la telefon sunt disponibile acum pe masina asta."""
    out = []
    try:
        from .afc import list_devices as afc_devices
        devices = afc_devices()
        out.append({"backend": "afc", "available": True,
                    "detail": f"{len(devices)} dispozitiv(e) pe USB", "devices": devices})
    except Exception as exc:
        out.append({"backend": "afc", "available": False, "detail": str(exc),
                    "devices": []})
    try:
        from .mtp import list_devices as mtp_devices
        names = mtp_devices()
        out.append({"backend": "mtp", "available": True,
                    "detail": f"{len(names)} dispozitiv(e) portabil(e)", "devices": names})
    except Exception as exc:
        out.append({"backend": "mtp", "available": False, "detail": str(exc),
                    "devices": []})
    return out
