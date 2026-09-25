"""Interfata comuna pentru sursele de poze: folder local sau iPhone pe USB.

Ideea centrala e `localize`: indiferent de unde vine fisierul, pipeline-ul
primeste o cale locala pe care o poate citi normal. Pentru un folder e chiar
fisierul original; pentru iPhone e o copie temporara in zona de staging de pe
SSD, ca mutarea la destinatie sa fie apoi instantanee (acelasi volum).
"""
from __future__ import annotations

import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class SourceItem:
    ref: str              # identificator unic si stabil in sursa (calea completa)
    name: str             # numele fisierului
    rel_dir: str          # folderul relativ in sursa, doar informativ
    size: int
    mtime: float | None = None
    kind: str = "folder"  # tipul sursei, salvat in baza de date

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
    claimed: bool = False  # pipeline-ul a mutat fisierul, nu-l mai stergem


class Source:
    """Sursa de fisiere. Subclasele implementeaza iter_items si _fetch."""

    kind = "base"
    label = "sursa"
    supports_random_read = False

    def iter_items(self) -> Iterator[SourceItem]:
        raise NotImplementedError

    def count_hint(self) -> int | None:
        """Numarul aproximativ de fisiere, daca se poate afla ieftin."""
        return None

    def read_range(self, item: SourceItem, offset: int, length: int) -> bytes:
        """Citire partiala, doar pentru sursele cu supports_random_read."""
        raise NotImplementedError

    def _fetch(self, item: SourceItem, target: Path) -> None:
        """Aduce fisierul la calea locala `target`."""
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


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
