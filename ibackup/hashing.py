"""File fingerprints: a cheap partial MD5, plus a full MD5 for confirmation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_CHUNK = 64 * 1024
READ_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Fingerprint:
    """A file's quick identity: size plus an MD5 over its head and tail.

    Two different files could in theory share a partial fingerprint, so the
    pipeline confirms a match with a full MD5 before calling it a duplicate
    (see `dedup.confirm_with_full_md5` in the config).
    """

    size: int
    partial_md5: str

    def as_key(self) -> tuple[int, str]:
        return (self.size, self.partial_md5)


def partial_md5_from_reader(
    read_at: Callable[[int, int], bytes], size: int, chunk: int = DEFAULT_CHUNK
) -> str:
    """MD5 over the first and last `chunk` bytes, via a random-access reader.

    `read_at(offset, length)` can read from disk or straight from the iPhone,
    which lets us recognise a duplicate without downloading the whole file.
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
