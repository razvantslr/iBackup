"""Amprente de fisier: MD5 partial (rapid) + MD5 complet (confirmare)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_CHUNK = 64 * 1024
READ_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Fingerprint:
    """Identitatea rapida a unui fisier: dimensiune + MD5 peste cap si coada.

    Doua fisiere diferite pot teoretic sa aiba aceeasi amprenta partiala, de
    aceea pipeline-ul confirma potrivirile cu MD5 complet inainte sa declare
    un duplicat (vezi `dedup.confirm_with_full_md5` din config).
    """

    size: int
    partial_md5: str

    def as_key(self) -> tuple[int, str]:
        return (self.size, self.partial_md5)


def partial_md5_from_reader(
    read_at: Callable[[int, int], bytes], size: int, chunk: int = DEFAULT_CHUNK
) -> str:
    """MD5 peste primii si ultimii `chunk` octeti, folosind un cititor random-access.

    `read_at(offset, length)` poate citi de pe disc sau direct de pe iPhone,
    ceea ce ne lasa sa detectam duplicatele fara sa descarcam fisierul intreg.
    """
    md5 = hashlib.md5()
    md5.update(str(size).encode("ascii"))
    if size <= 2 * chunk:
        md5.update(read_at(0, size))
    else:
        md5.update(read_at(0, chunk))
        md5.update(read_at(size - chunk, chunk))
    return md5.hexdigest()


def partial_md5(path: Path, size: int | None = None, chunk: int = DEFAULT_CHUNK) -> str:
    path = Path(path)
    if size is None:
        size = path.stat().st_size
    with path.open("rb") as fh:
        def read_at(offset: int, length: int) -> bytes:
            fh.seek(offset)
            return fh.read(length)

        return partial_md5_from_reader(read_at, size, chunk)


def file_md5(path: Path) -> str:
    md5 = hashlib.md5()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(READ_BLOCK), b""):
            md5.update(block)
    return md5.hexdigest()


def fingerprint(path: Path, chunk: int = DEFAULT_CHUNK) -> Fingerprint:
    path = Path(path)
    size = path.stat().st_size
    return Fingerprint(size=size, partial_md5=partial_md5(path, size, chunk))
