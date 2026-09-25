"""Small helpers: month names, filesystem-safe names, unique paths, free space."""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

# Folder names on disk stay in the user's language; only the code is English.
MONTHS = {
    "ro": {
        1: "ianuarie", 2: "februarie", 3: "martie", 4: "aprilie",
        5: "mai", 6: "iunie", 7: "iulie", 8: "august",
        9: "septembrie", 10: "octombrie", 11: "noiembrie", 12: "decembrie",
    },
    "en": {
        1: "january", 2: "february", 3: "march", 4: "april",
        5: "may", 6: "june", 7: "july", 8: "august",
        9: "september", 10: "october", 11: "november", 12: "december",
    },
}

# Characters that NFKD does not decompose on its own.
_EXTRA_FOLD = {
    "ß": "ss", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe",
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ł": "l", "Ł": "L", "ı": "i", "ʼ": "'",
}

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

GB = 1024 ** 3


def fold_ascii(text: str) -> str:
    """'Bucuresti' from 'Bucureşti', 'Malaga' from 'Málaga'.

    Folder names are kept ASCII so the same backup reads identically on
    Windows, on an exFAT-formatted SSD and on a Linux box, none of which agree
    on Unicode normalisation.
    """
    out = []
    for ch in text:
        if ch in _EXTRA_FOLD:
            out.append(_EXTRA_FOLD[ch])
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(stripped if stripped else ch)
    return "".join(out)


def sanitize_component(name: str, fallback: str = "_") -> str:
    """Turn arbitrary text into a valid Windows folder name."""
    name = fold_ascii(name).strip()
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_{2,}", "_", name)
    name = name.strip("._ ")
    if not name:
        return fallback
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name
    return name[:120]


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Like sanitize_component, but keeps the extension."""
    stem, ext = os.path.splitext(name)
    ext = _ILLEGAL.sub("_", fold_ascii(ext)).strip()
    stem = sanitize_component(stem, fallback=fallback)
    if len(ext) > 12:
        ext = ext[:12]
    return f"{stem}{ext}"


def month_folder(month: int, language: str = "ro") -> str:
    names = MONTHS.get(language, MONTHS["ro"])
    return f"{month:02d}_{names[month]}"


def unique_path(path: Path, taken: set[str] | None = None) -> Path:
    """Return a free path, appending _2, _3 ... when needed.

    `taken` lets the caller reserve paths chosen earlier in the same run,
    before the files actually exist on disk (needed for dry-run).
    """
    taken = taken if taken is not None else set()

    def busy(p: Path) -> bool:
        return p.exists() or str(p).lower() in taken

    if not busy(path):
        return path
    stem, ext = os.path.splitext(path.name)
    for i in range(2, 10000):
        candidate = path.with_name(f"{stem}_{i}{ext}")
        if not busy(candidate):
            return candidate
    raise RuntimeError(f"No free name found for {path}")


def free_space(path: Path) -> int:
    """Bytes available on the volume holding `path`.

    Walks up to the nearest existing ancestor, so this works before the backup
    folder has been created.
    """
    path = Path(path)
    for candidate in [path, *path.parents]:
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                break
    return 0


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
