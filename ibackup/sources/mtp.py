"""Sursa "iPhone prin MTP" - exact ce vezi in Explorer la Apple iPhone.

Merge cu driverul care vine cu Windows, fara iTunes, dar are limitele shell-ului:
copiem fisier cu fisier prin CopyHere si nu putem citi partial, deci fiecare
fisier trebuie adus pe disc inainte sa stim daca e duplicat. Pentru biblioteci
mari, AFC (sources/afc.py) e varianta buna.

ATENTIE: pe telefon trebuie setat Settings > Photos > Transfer to Mac or PC >
Keep Originals. Pe "Automatic", iPhone-ul converteste HEIC in JPEG in timpul
copierii si primesti alt fisier decat originalul.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from ..mediameta import is_media_name, kind_for_name
from .base import Source, SourceItem

SSF_DRIVES = 17  # "This PC" in spatiul de nume al shell-ului

# Flag-urile pentru CopyHere: fara dialoguri, fara confirmari, da la toate.
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
            "pywin32 nu e instalat. Ruleaza: pip install pywin32"
        ) from exc
    return pythoncom, win32com.client


def list_devices() -> list[str]:
    """Numele dispozitivelor portabile vazute de Windows."""
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

    def __init__(self, *, device_name: str | None = None, include_videos=True,
                 include_sidecars=True, copy_timeout=180.0):
        pythoncom, client = _import_win32()
        pythoncom.CoInitialize()
        self._client = client
        self.shell = client.Dispatch("Shell.Application")
        self.device = self._find_device(device_name)
        self.label = f"iPhone MTP ({self.device.Name})"
        self.include_videos = include_videos
        self.include_sidecars = include_sidecars
        self.copy_timeout = copy_timeout
        self._folders: dict[str, object] = {}

    def _find_device(self, device_name: str | None):
        computer = self.shell.NameSpace(SSF_DRIVES)
        if computer is None:
            raise MtpUnavailable("Nu pot deschide 'This PC' prin shell.")
        candidates = []
        for item in computer.Items():
            try:
                if not item.IsFolder:
                    continue
                path = str(item.Path)
                if len(path) > 1 and path[1:3] == ":\\":
                    continue  # partitie obisnuita, nu dispozitiv portabil
                candidates.append(item)
            except Exception:
                continue
        if device_name:
            for item in candidates:
                if device_name.lower() in str(item.Name).lower():
                    return item
            raise MtpUnavailable(f"Nu am gasit dispozitivul '{device_name}'.")
        for item in candidates:
            if "iphone" in str(item.Name).lower() or "ipad" in str(item.Name).lower():
                return item
        if candidates:
            return candidates[0]
        raise MtpUnavailable(
            "Nu vad niciun iPhone. Verifica: cablu de date, telefon deblocat, "
            "'Trust This Computer' apasat, si ca apare in Explorer."
        )

    # --- enumerare ---------------------------------------------------------

    def _folder_of(self, item):
        try:
            return item.GetFolder
        except Exception:
            return None

    def _accepts(self, name: str) -> bool:
        if not is_media_name(name):
            return False
        kind = kind_for_name(name)
        if kind == "video" and not self.include_videos:
            return False
        if kind == "sidecar" and not self.include_sidecars:
            return False
        return True

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

    # --- aducerea fisierului -----------------------------------------------

    def _resolve(self, item: SourceItem):
        folder = self._folders.get(item.rel_dir)
        if folder is None:
            raise MtpUnavailable(
                f"Folderul '{item.rel_dir}' nu mai e in cache; reconecteaza telefonul."
            )
        entry = folder.ParseName(item.name)
        if entry is None:
            raise FileNotFoundError(f"{item.ref} nu mai exista pe telefon")
        return entry

    def _fetch(self, item: SourceItem, target: Path) -> None:
        """CopyHere pastreaza numele original, deci copiem intr-un folder propriu."""
        entry = self._resolve(item)
        scratch = target.parent / f"{target.stem}_mtp"
        scratch.mkdir(parents=True, exist_ok=True)
        dest_folder = self.shell.NameSpace(str(scratch))
        if dest_folder is None:
            raise MtpUnavailable(f"Nu pot deschide folderul de staging {scratch}")
        dest_folder.CopyHere(entry, COPY_FLAGS)

        produced = self._wait_for_copy(scratch, item)
        produced.replace(target)
        try:
            scratch.rmdir()
        except OSError:
            pass

    def _wait_for_copy(self, scratch: Path, item: SourceItem) -> Path:
        """CopyHere e asincron: asteptam sa apara fisierul si sa nu mai creasca."""
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
        raise TimeoutError(f"Copierea lui {item.name} de pe telefon a expirat")

    def close(self) -> None:
        self._folders.clear()
