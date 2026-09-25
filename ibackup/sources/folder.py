"""The "folder" source: an old backup, an SD card, any directory on disk."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .base import Localized, Source, SourceItem, accepts_kind


class FolderSource(Source):
    kind = "folder"
    supports_random_read = True

    def __init__(self, root, *, skip_dirs=None, accept_kinds=None,
                 follow_symlinks=False):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"The source is not a folder: {self.root}")
        self.label = str(self.root)
        self.skip_dirs = {d.lower() for d in (skip_dirs or [])}
        self.accept_kinds = set(accept_kinds) if accept_kinds is not None else None
        self.follow_symlinks = follow_symlinks

    def _accepts(self, name: str) -> bool:
        return accepts_kind(name, self.accept_kinds)

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
        # The file is already on disk; no point copying it into staging.
        yield Localized(path=Path(item.ref), is_temp=False)
