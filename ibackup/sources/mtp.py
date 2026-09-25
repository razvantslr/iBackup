"""The "iPhone over MTP" source - what Explorer shows under Apple iPhone.

It works with the driver Windows ships, without iTunes, but it inherits the
shell's limits: files are copied one by one through CopyHere and cannot be read
partially, so every file must be fetched before we can tell whether it is a
duplicate. For large libraries, AFC (sources/afc.py) is the better choice.

NOTE: on the phone, set Settings > Photos > Transfer to Mac or PC > Keep
Originals. On "Automatic" the iPhone converts HEIC to JPEG while copying and you
receive a different file from the original.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from .base import Source, SourceItem, accepts_kind

SSF_DRIVES = 17  # "This PC" in the shell namespace

# CopyHere flags: no dialogs, no confirmations, yes to everything.
COPY_FLAGS = 4 | 16 | 512 | 1024

MEDIA_DIR_HINTS = ("dcim", "internal storage", "apple")


class MtpUnavailable(RuntimeError):
    pass


def _import_win32():
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:  # pragma: no cover - depinde de mediu
        raise MtpUnavailable(
            "pywin32 is not installed. Run: pip install pywin32"
        ) from exc
    return pythoncom, win32com.client


def list_devices() -> list[str]:
    """The names of the portable devices Windows can see."""
    pythoncom, client = _import_win32()
    pythoncom.CoInitialize()
    shell = client.Dispatch("Shell.Application")
    computer = shell.NameSpace(SSF_DRIVES)
    names = []
    if computer is None:
        return names
    for item in computer.Items():
        try:
            if item.IsFolder and not str(item.Path)[1:3] == ":\\":
                names.append(str(item.Name))
        except Exception:
            continue
    return names


class MtpSource(Source):
    kind = "mtp"
    supports_random_read = False

    def __init__(self, *, device_name: str | None = None, accept_kinds=None,
                 copy_timeout=180.0):
        pythoncom, client = _import_win32()
        pythoncom.CoInitialize()
        self._client = client
        self.shell = client.Dispatch("Shell.Application")
        self.device = self._find_device(device_name)
        self.label = f"iPhone MTP ({self.device.Name})"
        self.accept_kinds = set(accept_kinds) if accept_kinds is not None else None
        self.copy_timeout = copy_timeout
        self._folders: dict[str, object] = {}

    def _find_device(self, device_name: str | None):
        computer = self.shell.NameSpace(SSF_DRIVES)
        if computer is None:
            raise MtpUnavailable("Cannot open 'This PC' through the shell.")
        candidates = []
        for item in computer.Items():
            try:
                if not item.IsFolder:
                    continue
                path = str(item.Path)
                if len(path) > 1 and path[1:3] == ":\\":
                    continue  # an ordinary drive, not a portable device
                candidates.append(item)
            except Exception:
                continue
        if device_name:
            for item in candidates:
                if device_name.lower() in str(item.Name).lower():
                    return item
            raise MtpUnavailable(f"Device '{device_name}' not found.")
        for item in candidates:
            if "iphone" in str(item.Name).lower() or "ipad" in str(item.Name).lower():
                return item
        if candidates:
            return candidates[0]
        raise MtpUnavailable(
            "No iPhone visible. Check: a data cable, the phone unlocked, "
            "'Trust This Computer' accepted, and that it shows up in Explorer."
        )

    # --- enumeration -------------------------------------------------------

    def _folder_of(self, item):
        try:
            return item.GetFolder
        except Exception:
            return None

    def _accepts(self, name: str) -> bool:
        return accepts_kind(name, self.accept_kinds)

    def _size_of(self, item) -> int:
        for prop in ("System.Size",):
            try:
                value = item.ExtendedProperty(prop)
                if value:
                    return int(value)
            except Exception:
                continue
        try:
            return int(item.Size)
        except Exception:
            return 0

    def _mtime_of(self, item):
        for prop in ("System.DateModified", "System.Photo.DateTaken"):
            try:
                value = item.ExtendedProperty(prop)
                if value is not None and hasattr(value, "timestamp"):
                    return value.timestamp()
            except Exception:
                continue
        return None

    def _walk(self, folder, rel_dir: str, depth: int) -> Iterator[SourceItem]:
        if folder is None or depth > 6:
            return
        self._folders[rel_dir] = folder
        try:
            items = list(folder.Items())
        except Exception:
            return
        for item in items:
            try:
                name = str(item.Name)
                if item.IsFolder:
                    child_rel = f"{rel_dir}/{name}" if rel_dir else name
                    yield from self._walk(self._folder_of(item), child_rel, depth + 1)
                    continue
                if not self._accepts(name):
                    continue
                size = self._size_of(item)
                yield SourceItem(
                    ref=f"{rel_dir}/{name}" if rel_dir else name,
                    name=name, rel_dir=rel_dir, size=size,
                    mtime=self._mtime_of(item), kind=self.kind,
                )
            except Exception:
                continue

    def iter_items(self) -> Iterator[SourceItem]:
        root = self._folder_of(self.device)
        yield from self._walk(root, "", 0)

    # --- fetching a file ---------------------------------------------------

    def _resolve(self, item: SourceItem):
        folder = self._folders.get(item.rel_dir)
        if folder is None:
            raise MtpUnavailable(
                f"Folder '{item.rel_dir}' is no longer cached; reconnect the phone."
            )
        entry = folder.ParseName(item.name)
        if entry is None:
            raise FileNotFoundError(f"{item.ref} no longer exists on the phone")
        return entry

    def _fetch(self, item: SourceItem, target: Path) -> None:
        """CopyHere keeps the original name, so we copy into a folder of our own."""
        entry = self._resolve(item)
        scratch = target.parent / f"{target.stem}_mtp"
        scratch.mkdir(parents=True, exist_ok=True)
        dest_folder = self.shell.NameSpace(str(scratch))
        if dest_folder is None:
            raise MtpUnavailable(f"Cannot open the staging folder {scratch}")
        dest_folder.CopyHere(entry, COPY_FLAGS)

        produced = self._wait_for_copy(scratch, item)
        produced.replace(target)
        try:
            scratch.rmdir()
        except OSError:
            pass

    def _wait_for_copy(self, scratch: Path, item: SourceItem) -> Path:
        """CopyHere is asynchronous: wait for the file to appear and stop growing."""
        deadline = time.monotonic() + self.copy_timeout
        last_size, stable_since = -1, None
        while time.monotonic() < deadline:
            files = [p for p in scratch.iterdir() if p.is_file()]
            if files:
                candidate = files[0]
                try:
                    size = candidate.stat().st_size
                except OSError:
                    size = -1
                if size > 0 and size == last_size:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= 0.4:
                        return candidate
                else:
                    stable_since = None
                last_size = size
            time.sleep(0.15)
        raise TimeoutError(f"Copying {item.name} from the phone timed out")

    def close(self) -> None:
        self._folders.clear()
