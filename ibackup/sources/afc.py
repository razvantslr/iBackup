"""The "iPhone over AFC" source - Apple's own protocol, via pymobiledevice3.

This is the preferred backend: it sees the original files in /DCIM, supports
partial reads (so duplicates are detected without downloading the whole photo)
and is much faster than MTP. It needs the Apple Mobile Device service installed
on Windows (it ships with the "Apple Devices" app from the Microsoft Store, or
with iTunes) and the phone unlocked with "Trust This Computer" accepted.

pymobiledevice3 is optional: the imports are lazy so the tool still starts
without it.
"""
from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Iterator

from .base import Source, SourceItem, accepts_kind

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
            "pymobiledevice3 is not installed. Run: pip install pymobiledevice3"
        ) from exc
    return create_using_usbmux, AfcService


def list_devices() -> list[dict]:
    """The phones visible over USB, for the `devices` command."""
    try:
        from pymobiledevice3.usbmux import list_devices as _list  # type: ignore
    except ImportError as exc:
        raise AfcUnavailable(
            "pymobiledevice3 is not installed. Run: pip install pymobiledevice3"
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
                 accept_kinds=None):
        super().__init__()
        create_using_usbmux, AfcService = _import_pymobiledevice3()
        try:
            self.lockdown = create_using_usbmux(serial=serial)
        except Exception as exc:
            raise AfcUnavailable(
                "No connected iPhone found. Check: a data cable, the phone "
                "unlocked, and 'Trust This Computer' accepted."
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
        self.label = f"iPhone AFC ({name or 'unknown'})"
        self.roots = list(roots)
        self.accept_kinds = set(accept_kinds) if accept_kinds is not None else None
        self._supports_seek = all(
            hasattr(self.afc, attr) for attr in ("fopen", "fread", "fclose")
        )
        if not self._supports_seek:
            self.supports_random_read = False

    # --- enumeration -------------------------------------------------------

    def _stat(self, path: str) -> dict:
        try:
            info = self.afc.stat(path)
        except Exception as exc:
            self._skip(path, f"cannot stat: {type(exc).__name__}: {exc}")
            return {}
        if not info:
            self._skip(path, "stat returned nothing")
        return info or {}

    def _is_dir(self, info: dict) -> bool:
        return str(info.get("st_ifmt", "")).upper().endswith("IFDIR")

    def _accepts(self, name: str) -> bool:
        return accepts_kind(name, self.accept_kinds)

    def _walk(self, path: str, depth: int) -> Iterator[SourceItem]:
        if depth > MAX_DEPTH:
            self._skip(path, f"deeper than {MAX_DEPTH} levels")
            return
        try:
            names = sorted(self.afc.listdir(path))
        except Exception as exc:
            self._skip(path, f"folder not readable: {type(exc).__name__}: {exc}")
            return
        for name in names:
            if name in (".", ".."):
                continue
            child = posixpath.join(path, name)
            info = self._stat(child)
            if not info:
                continue  # _stat noted it
            if self._is_dir(info):
                yield from self._walk(child, depth + 1)
                continue
            if not self._accepts(name):
                self._ignore(name)
                continue
            size = int(info.get("st_size") or 0)
            if size <= 0:
                self._skip(child, "empty file")
                continue
            mtime = info.get("st_mtime")
            stamp = None
            if hasattr(mtime, "timestamp"):
                try:
                    stamp = mtime.timestamp()
                except Exception:
                    stamp = None
            elif isinstance(mtime, (int, float)):
                # AFC sometimes reports nanoseconds.
                stamp = float(mtime) / 1e9 if mtime > 1e12 else float(mtime)
            yield SourceItem(ref=child, name=name, rel_dir=path, size=size,
                             mtime=stamp, kind=self.kind)

    def iter_items(self) -> Iterator[SourceItem]:
        for root in self.roots:
            yield from self._walk(root, 0)

    # --- reading -----------------------------------------------------------

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
