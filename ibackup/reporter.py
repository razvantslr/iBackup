"""Afisarea progresului si jurnalul fiecarei rulari."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

from .util import human_duration, human_size


class Reporter:
    def __init__(self, log_path: Path | None = None, quiet: bool = False,
                 verbose: bool = False):
        self.quiet = quiet
        self.verbose = verbose
        self.started = time.monotonic()
        self._last_progress = 0.0
        self._log = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open("a", encoding="utf-8")
            self._write_log(f"=== rulare noua {datetime.now().isoformat(timespec='seconds')} ===")

    # --- iesire ------------------------------------------------------------

    def _write_log(self, text: str) -> None:
        if self._log is not None:
            self._log.write(text + "\n")
            self._log.flush()

    def info(self, text: str) -> None:
        self._write_log(text)
        if not self.quiet:
            self._clear_line()
            print(text)

    def detail(self, text: str) -> None:
        self._write_log(text)
        if self.verbose and not self.quiet:
            self._clear_line()
            print(text)

    def warn(self, text: str) -> None:
        self._write_log("ATENTIE: " + text)
        if not self.quiet:
            self._clear_line()
            print(f"  ! {text}", file=sys.stderr)

    def error(self, text: str) -> None:
        self._write_log("EROARE: " + text)
        self._clear_line()
        print(f"  x {text}", file=sys.stderr)

    def _clear_line(self) -> None:
        if not self.quiet and self._last_progress:
            print("\r" + " " * 78 + "\r", end="")
            self._last_progress = 0.0

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        # In modul verbose fiecare fisier isi are randul lui; o bara de progres
        # care se rescrie peste ele ar face iesirea ilizibila.
        if self.quiet or self.verbose:
            return
        now = time.monotonic()
        if now - self._last_progress < 0.2:
            return
        self._last_progress = now
        if total:
            pct = 100.0 * done / total
            text = f"\r  {done}/{total} ({pct:.0f}%) {label}"
        else:
            text = f"\r  {done} fisiere {label}"
        print(text[:78].ljust(78), end="", flush=True)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    LABELS = {
        "scanned": "fisiere vazute in sursa",
        "imported": "copiate in backup",
        "duplicates": "duplicate sarite",
        "companions": "din care fisiere pereche (Live Photo / .AAE)",
        "no_exif_date": "fara data in metadate (am folosit nume/mtime)",
        "no_gps": "fara coordonate GPS",
        "inferred_places": "locatii deduse din pozele vecine",
        "errors": "erori",
        "checked": "fisiere verificate",
        "missing": "lipsesc din backup",
        "size_mismatch": "dimensiune diferita fata de index",
        "hash_mismatch": "continut schimbat fata de index",
        "seen": "fisiere gasite in backup",
        "added": "adaugate in index",
        "already": "erau deja in index",
    }

    def summary(self, title: str, stats: dict) -> None:
        self._clear_line()
        lines = [f"\n{title}"]
        ordered = [k for k in self.LABELS if k in stats]
        ordered += [k for k in stats if k not in self.LABELS and k != "bytes_copied"]
        for key in ordered:
            label = self.LABELS.get(key, key)
            lines.append(f"  {label:<48} {stats[key]}")
        if "bytes_copied" in stats:
            lines.append(f"  {'total copiat':<48} {human_size(stats['bytes_copied'])}")
        lines.append(f"  {'durata':<48} {human_duration(self.elapsed())}")
        text = "\n".join(lines)
        self._write_log(text)
        if not self.quiet:
            print(text)

    def close(self) -> None:
        self._clear_line()
        if self._log is not None:
            self._log.close()
            self._log = None
