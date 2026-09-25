"""Sursa "folder": un backup vechi, un card SD, orice director de pe disc."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..mediameta import is_media_name
from .base import Localized, Source, SourceItem


class FolderSource(Source):
    kind = "folder"
    supports_random_read = True

    def __init__(self, root, *, skip_dirs=None, include_videos=True,
                 include_sidecars=True, follow_symlinks=False):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"Sursa nu e un folder: {self.root}")
        self.label = str(self.root)
        self.skip_dirs = {d.lower() for d in (skip_dirs or [])}
        self.include_videos = include_videos
        self.include_sidecars = include_sidecars
        self.follow_symlinks = follow_symlinks

    def _accepts(self, name: str) -> bool:
        from ..mediameta import kind_for_name
        if not is_media_name(name):
            return False
        kind = kind_for_name(name)
        if kind == "video" and not self.include_videos:
            return False
        if kind == "sidecar" and not self.include_sidecars:
            return False
        return True

    def iter_items(self) -> Iterator[SourceItem]:
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=self.follow_symlinks):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in self.skip_dirs)
            rel_dir = os.path.relpath(dirpath, self.root).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
            for name in sorted(filenames):
                if not self._accepts(name):
                    continue
                full = Path(dirpath) / name
                try:
                    stat = full.stat()
                except OSError:
                    continue
                if stat.st_size == 0:
                    continue
                yield SourceItem(
                    ref=str(full), name=name, rel_dir=rel_dir,
                    size=stat.st_size, mtime=stat.st_mtime, kind=self.kind,
                )

    def read_range(self, item: SourceItem, offset: int, length: int) -> bytes:
        with open(item.ref, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    @contextmanager
    def localize(self, item: SourceItem, staging_dir: Path):
        # Fisierul e deja pe disc: nu-l copiem degeaba in staging.
        yield Localized(path=Path(item.ref), is_temp=False)
