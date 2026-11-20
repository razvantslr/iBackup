"""The common interface for photo sources: a local folder, or an iPhone on USB.

The central idea is `localize`: wherever a file comes from, the pipeline gets a
local path it can read normally. For a folder that is the original file; for an
iPhone it is a temporary copy in the staging area on the SSD, so that moving it
to its final place afterwards is instant (same volume).
"""
from __future__ import annotations

import os
import shutil
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class SourceItem:
    ref: str              # unique, stable id within the source (the full path)
    name: str             # the file name
    rel_dir: str          # folder inside the source, informational only
    size: int
    mtime: float | None = None
    kind: str = "folder"  # source type, stored in the index

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


@dataclass
class Localized:
    path: Path
    is_temp: bool
    claimed: bool = False  # the pipeline moved the file; do not delete it


class Source:
    """A source of files. Subclasses implement iter_items and _fetch."""

    kind = "base"
    label = "source"
    supports_random_read = False

    def __init__(self):
        # What the walk could not read, and the non-media files it passed over.
        # The pipeline writes both to the run log.
        self.skipped: list[tuple[str, str]] = []
        self.ignored: Counter[str] = Counter()

    def _skip(self, path, reason: str) -> None:
        self.skipped.append((str(path), reason))

    def _ignore(self, name: str) -> None:
        from ..mediameta import is_media_name

        if not is_media_name(name):
            self.ignored[os.path.splitext(name)[1].lower() or "(no extension)"] += 1

    def iter_items(self) -> Iterator[SourceItem]:
        raise NotImplementedError

    def count_hint(self) -> int | None:
        """Roughly how many files there are, when that is cheap to know."""
        return None

    def read_range(self, item: SourceItem, offset: int, length: int) -> bytes:
        """Partial read, only for sources with supports_random_read."""
        raise NotImplementedError

    def local_path(self, item: SourceItem) -> Path | None:
        """The item's real path on this machine, when it has one.

        Only a folder source does; for a phone the file exists nowhere locally
        until it is fetched. The pipeline uses this to decide whether it can
        confirm a duplicate with a full MD5 for free.
        """
        return None

    def _fetch(self, item: SourceItem, target: Path) -> None:
        """Bring the file to the local path `target`."""
        raise NotImplementedError

    @contextmanager
    def localize(self, item: SourceItem, staging_dir: Path) -> Iterator[Localized]:
        staging_dir = Path(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        temp = staging_dir / f"{uuid.uuid4().hex}{item.ext or '.tmp'}"
        local = Localized(path=temp, is_temp=True)
        try:
            self._fetch(item, temp)
            yield local
        finally:
            if local.is_temp and not local.claimed and temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def accepts_kind(name: str, accept_kinds: set[str] | None) -> bool:
    """Whether a file should be enumerated at all, judging only by its name.

    Screenshots cannot be recognised from the name alone, so a source keeps all
    photos whenever either photos or screenshots are wanted; the pipeline makes
    the final call once it has read the metadata.
    """
    from ..mediameta import is_media_name, kind_for_name

    if not is_media_name(name):
        return False
    if accept_kinds is None:
        return True
    kind = kind_for_name(name)
    if kind == "photo":
        return "photo" in accept_kinds or "screenshot" in accept_kinds
    return kind in accept_kinds


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
