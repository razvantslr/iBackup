"""Sursa "iPhone prin AFC" - protocolul Apple, via pymobiledevice3.

E varianta preferata pentru iPhone: vede fisierele originale din /DCIM, permite
citiri partiale (deci detectam duplicatele fara sa descarcam poza intreaga) si e
mult mai rapida decat MTP. Cere serviciul Apple Mobile Device instalat pe
Windows (vine cu aplicatia "Apple Devices" din Microsoft Store sau cu iTunes) si
telefonul deblocat, cu "Trust This Computer" apasat.

Pachetul pymobiledevice3 e optional: importurile se fac lenes, ca tool-ul sa
porneasca si fara el.
"""
from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Iterator

from ..mediameta import is_media_name, kind_for_name
from .base import Source, SourceItem

DEFAULT_ROOTS = ("/DCIM",)
MAX_DEPTH = 6


class AfcUnavailable(RuntimeError):
    pass


def _import_pymobiledevice3():
    try:
        from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore
        from pymobiledevice3.services.afc import AfcService  # type: ignore
    except ImportError as exc:  # pragma: no cover - depinde de mediu
        raise AfcUnavailable(
            "pymobiledevice3 nu e instalat. Ruleaza: pip install pymobiledevice3"
        ) from exc
    return create_using_usbmux, AfcService


def list_devices() -> list[dict]:
    """Telefoanele vazute pe USB, pentru comanda `devices`."""
    try:
        from pymobiledevice3.usbmux import list_devices as _list  # type: ignore
    except ImportError as exc:
        raise AfcUnavailable(
            "pymobiledevice3 nu e instalat. Ruleaza: pip install pymobiledevice3"
        ) from exc
    out = []
    for device in _list():
        out.append({
            "serial": getattr(device, "serial", None),
            "connection": getattr(device, "connection_type", None),
            "device_id": getattr(device, "devid", None),
        })
    return out


class AfcSource(Source):
    kind = "afc"
    supports_random_read = True

    def __init__(self, *, serial: str | None = None, roots=DEFAULT_ROOTS,
                 include_videos=True, include_sidecars=True):
        create_using_usbmux, AfcService = _import_pymobiledevice3()
        try:
            self.lockdown = create_using_usbmux(serial=serial)
        except Exception as exc:
            raise AfcUnavailable(
                "Nu am gasit un iPhone conectat. Verifica: cablu de date, telefon "
                "deblocat, 'Trust This Computer' apasat."
            ) from exc
        try:
            self.afc = AfcService(lockdown=self.lockdown)
        except TypeError:
            self.afc = AfcService(self.lockdown)

        name = None
        for attr in ("display_name", "device_name"):
            name = name or getattr(self.lockdown, attr, None)
        try:
            name = name or self.lockdown.all_values.get("DeviceName")
        except Exception:
            pass
        self.label = f"iPhone AFC ({name or 'necunoscut'})"
        self.roots = list(roots)
        self.include_videos = include_videos
        self.include_sidecars = include_sidecars
        self._supports_seek = all(
            hasattr(self.afc, attr) for attr in ("fopen", "fread", "fclose")
        )
        if not self._supports_seek:
            self.supports_random_read = False

    # --- enumerare ---------------------------------------------------------

    def _stat(self, path: str) -> dict:
        try:
            return self.afc.stat(path) or {}
        except Exception:
            return {}

    def _is_dir(self, info: dict) -> bool:
        return str(info.get("st_ifmt", "")).upper().endswith("IFDIR")

    def _accepts(self, name: str) -> bool:
        if not is_media_name(name):
            return False
        kind = kind_for_name(name)
        if kind == "video" and not self.include_videos:
            return False
        if kind == "sidecar" and not self.include_sidecars:
            return False
        return True

    def _walk(self, path: str, depth: int) -> Iterator[SourceItem]:
        if depth > MAX_DEPTH:
            return
        try:
            names = sorted(self.afc.listdir(path))
        except Exception:
            return
        for name in names:
            if name in (".", ".."):
                continue
            child = posixpath.join(path, name)
            info = self._stat(child)
            if self._is_dir(info):
                yield from self._walk(child, depth + 1)
                continue
            if not self._accepts(name):
                continue
            size = int(info.get("st_size") or 0)
            if size <= 0:
                continue
            mtime = info.get("st_mtime")
            stamp = None
            if hasattr(mtime, "timestamp"):
                try:
                    stamp = mtime.timestamp()
                except Exception:
                    stamp = None
            elif isinstance(mtime, (int, float)):
                # AFC raporteaza uneori nanosecunde.
                stamp = float(mtime) / 1e9 if mtime > 1e12 else float(mtime)
            yield SourceItem(ref=child, name=name, rel_dir=path, size=size,
                             mtime=stamp, kind=self.kind)

    def iter_items(self) -> Iterator[SourceItem]:
        for root in self.roots:
            yield from self._walk(root, 0)

    # --- citire ------------------------------------------------------------

    def read_range(self, item: SourceItem, offset: int, length: int) -> bytes:
        if not self._supports_seek:
            return self.afc.get_file_contents(item.ref)[offset:offset + length]
        handle = self.afc.fopen(item.ref, "r")
        try:
            if offset and hasattr(self.afc, "fseek"):
                self.afc.fseek(handle, offset, 0)
                return self.afc.fread(handle, length)
            data = self.afc.fread(handle, offset + length)
            return data[offset:offset + length]
        finally:
            try:
                self.afc.fclose(handle)
            except Exception:
                pass

    def _fetch(self, item: SourceItem, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.afc.get_file_contents(item.ref)
        target.write_bytes(data)

    def close(self) -> None:
        for obj in (getattr(self, "afc", None), getattr(self, "lockdown", None)):
            try:
                if obj is not None and hasattr(obj, "close"):
                    obj.close()
            except Exception:
                pass
