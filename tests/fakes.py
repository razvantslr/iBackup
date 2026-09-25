"""Stand-ins for the two iPhone transports, so their logic can be tested.

Neither backend can be exercised against a real device here, but almost none of
the code in them is protocol: it is directory walking, timestamp conversion,
partial reads and staging. These fakes present the same shapes those modules
program against - the pymobiledevice3 AfcService API, and the Windows shell COM
objects - backed by an ordinary local directory.

What they do not prove is that the real APIs behave as assumed. That still needs
a phone.
"""
from __future__ import annotations

import posixpath
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path


# --- AFC --------------------------------------------------------------------

class _AfcBase:
    """The part of AfcService that every version we support provides."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = Counter()

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def listdir(self, path: str) -> list[str]:
        self.calls["listdir"] += 1
        local = self._local(path)
        if not local.is_dir():
            raise OSError(f"no such directory: {path}")
        return sorted(p.name for p in local.iterdir())

    def stat(self, path: str) -> dict:
        self.calls["stat"] += 1
        local = self._local(path)
        if not local.exists():
            raise OSError(f"no such file: {path}")
        info = local.stat()
        return {
            "st_size": info.st_size,
            "st_ifmt": "S_IFDIR" if local.is_dir() else "S_IFREG",
            "st_mtime": datetime.fromtimestamp(info.st_mtime),
        }

    def get_file_contents(self, path: str) -> bytes:
        self.calls["get_file_contents"] += 1
        return self._local(path).read_bytes()

    def close(self) -> None:
        self.calls["close"] += 1


class FakeAfcService(_AfcBase):
    """A device whose AFC supports seeking, so partial reads are possible."""

    def __init__(self, root):
        super().__init__(root)
        self._handles: dict[int, object] = {}
        self._next = 1

    def fopen(self, path: str, mode: str = "r"):
        self.calls["fopen"] += 1
        handle = self._next
        self._next += 1
        self._handles[handle] = self._local(path).open("rb")
        return handle

    def fseek(self, handle, offset, whence=0):
        self.calls["fseek"] += 1
        self._handles[handle].seek(offset, whence)

    def fread(self, handle, size):
        self.calls["fread"] += 1
        return self._handles[handle].read(size)

    def fclose(self, handle):
        self.calls["fclose"] += 1
        self._handles.pop(handle).close()


class FakeAfcServiceNoSeek(_AfcBase):
    """An older device: whole-file reads only."""


class FakeLockdown:
    def __init__(self, name="Razvan's iPhone"):
        self.display_name = name
        self.all_values = {"DeviceName": name}
        self.closed = False

    def close(self):
        self.closed = True


def afc_import_hook(service, *, lockdown=None):
    """A replacement for afc._import_pymobiledevice3 that yields the fakes."""
    device = lockdown or FakeLockdown()

    def create_using_usbmux(serial=None, **kwargs):
        if service is None:
            raise ConnectionError("no device")
        return device

    def afc_service(lockdown=None, **kwargs):
        return service

    return lambda: (create_using_usbmux, afc_service)


# --- MTP / Windows shell ----------------------------------------------------

class FakeShellItem:
    """One entry in a shell folder: a file or a directory."""

    def __init__(self, path: Path, shell, virtual_prefix: str = ""):
        self._path = Path(path)
        self._shell = shell
        self._virtual_prefix = virtual_prefix

    @property
    def Name(self) -> str:
        return self._path.name

    @property
    def IsFolder(self) -> bool:
        return self._path.is_dir()

    @property
    def Path(self) -> str:
        # A portable device reports a shell path, not a drive-letter one.
        return f"{self._virtual_prefix}{self._path}" if self._virtual_prefix \
            else str(self._path)

    @property
    def GetFolder(self):
        return FakeShellFolder(self._path, self._shell, self._virtual_prefix)

    @property
    def Size(self) -> int:
        return self._path.stat().st_size

    def ExtendedProperty(self, name: str):
        if name == "System.Size":
            return self._path.stat().st_size
        if name == "System.DateModified":
            return datetime.fromtimestamp(self._path.stat().st_mtime)
        return None


class FakeShellFolder:
    def __init__(self, path: Path, shell, virtual_prefix: str = ""):
        self._path = Path(path)
        self._shell = shell
        self._virtual_prefix = virtual_prefix

    def Items(self):
        return [FakeShellItem(p, self._shell, self._virtual_prefix)
                for p in sorted(self._path.iterdir())]

    def ParseName(self, name: str):
        candidate = self._path / name
        if not candidate.exists():
            return None
        return FakeShellItem(candidate, self._shell, self._virtual_prefix)

    def CopyHere(self, item, flags=0):
        """The real call is asynchronous; copying straight away is a fair stand-in."""
        self._shell.copies += 1
        if self._shell.refuse_copies:
            return
        shutil.copy2(item._path, self._path / item._path.name)


class FakeShell:
    """Shell.Application, with one portable device plugged in."""

    SSF_DRIVES = 17

    def __init__(self, device_root: Path, device_name="Apple iPhone",
                 refuse_copies=False):
        self.device_root = Path(device_root)
        self.device_name = device_name
        self.refuse_copies = refuse_copies
        self.copies = 0

    def NameSpace(self, target):
        if target == self.SSF_DRIVES:
            return _FakeComputer(self)
        path = Path(str(target))
        return FakeShellFolder(path, self) if path.is_dir() else None


class _FakeComputer:
    """What "This PC" shows: one real drive plus the phone."""

    def __init__(self, shell: FakeShell):
        self._shell = shell

    def Items(self):
        drive = _FakeDriveItem("C:\\")
        device = FakeShellItem(self._shell.device_root, self._shell,
                               virtual_prefix="::{fake-portable-device}\\")
        device = _RenamedItem(device, self._shell.device_name)
        return [drive, device]


class _FakeDriveItem:
    def __init__(self, path):
        self._p = path

    @property
    def Name(self):
        return "Local Disk (C:)"

    @property
    def IsFolder(self):
        return True

    @property
    def Path(self):
        return self._p


class _RenamedItem:
    """The phone shows up under its device name, not its folder name."""

    def __init__(self, item: FakeShellItem, name: str):
        self._item = item
        self._name = name

    @property
    def Name(self):
        return self._name

    def __getattr__(self, attr):
        return getattr(self._item, attr)


class FakePythoncom:
    def __init__(self):
        self.initialised = 0

    def CoInitialize(self):
        self.initialised += 1


class FakeWin32Client:
    def __init__(self, shell: FakeShell):
        self._shell = shell

    def Dispatch(self, name: str):
        assert name == "Shell.Application"
        return self._shell


def mtp_import_hook(shell: FakeShell):
    """A replacement for mtp._import_win32 that yields the fakes."""
    return lambda: (FakePythoncom(), FakeWin32Client(shell))


def build_dcim(root: Path, files: dict[str, bytes]) -> Path:
    """Lay out a /DCIM tree: {"100APPLE/IMG_0001.HEIC": b"..."}."""
    for rel, payload in files.items():
        target = root / "DCIM" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root
