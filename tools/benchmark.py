"""Measure iBackup against a synthetic camera roll of realistic shape.

The v0.1 requirements asked for 10,000+ files (NFR-02). This builds a library
with that many, in roughly the proportions a phone produces, and times each
phase: the cold import, a warm re-import that should hit the source cache,
verification, and a no-op regeocode.

Payloads are small by default, which is the point: it isolates the per-file
overhead - metadata parsing, fingerprinting, geocoding, path building, SQLite -
from raw disk throughput, and that overhead is what decides whether a large
library is pleasant or painful. Pass --payload-kb to mix real I/O back in.

    python tools/benchmark.py --files 10000
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibackup import pipeline                                    # noqa: E402
from ibackup.config import Config                               # noqa: E402
from ibackup.db import Database                                 # noqa: E402
from ibackup.geocode import Geocoder                            # noqa: E402
from ibackup.reporter import Reporter                           # noqa: E402
from ibackup.sources.folder import FolderSource                 # noqa: E402
from ibackup.util import human_duration, human_size             # noqa: E402
from tests import synth                                         # noqa: E402

# A handful of places, so the geocoder's cache behaves as it would in life.
PLACES = [
    (41.3901, 2.1701), (44.4268, 26.1025), (45.35, 25.55), (48.8566, 2.3522),
    (51.5074, -0.1278), (45.65, 25.61), (46.77, 23.60), (37.9838, 23.7275),
]

# Roughly the shape of a real camera roll.
SHARE_PHOTO_GPS = 0.45
SHARE_PHOTO_NO_GPS = 0.15
SHARE_SCREENSHOT = 0.20
SHARE_VIDEO = 0.10
SHARE_LIVE = 0.08
SHARE_DUPLICATE = 0.02


def build_library(root: Path, count: int, payload_kb: int, seed: int = 7) -> dict:
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    payload = b"\xa5" * (payload_kb * 1024)
    start = datetime(2018, 1, 1, 8, 0, 0)
    made = {"photo": 0, "screenshot": 0, "video": 0, "live": 0, "duplicate": 0}
    first_photo: Path | None = None

    for index in range(count):
        # Files land in dated folders, a few hundred per folder, like a real roll.
        folder = root / f"{100 + index // 400}APPLE"
        folder.mkdir(parents=True, exist_ok=True)
        when = start + timedelta(minutes=17 * index)
        roll = rng.random()
        body = payload + str(index).encode()

        if roll < SHARE_PHOTO_GPS:
            synth.write_photo(folder / f"IMG_{index:05d}.JPG", when,
                              rng.choice(PLACES), payload=body)
            made["photo"] += 1
            first_photo = first_photo or folder / f"IMG_{index:05d}.JPG"
        elif roll < SHARE_PHOTO_GPS + SHARE_PHOTO_NO_GPS:
            synth.write_photo(folder / f"IMG_{index:05d}.JPG", when, None,
                              payload=body)
            made["photo"] += 1
        elif roll < SHARE_PHOTO_GPS + SHARE_PHOTO_NO_GPS + SHARE_SCREENSHOT:
            # Vary the dimensions so the screenshots are not byte-identical to
            # each other - otherwise the deduplicator quite correctly collapses
            # them all into one and the run measures nothing.
            synth.write_png(folder / f"IMG_{index:05d}.PNG",
                            width=1000 + index, height=2000)
            made["screenshot"] += 1
        elif roll < (SHARE_PHOTO_GPS + SHARE_PHOTO_NO_GPS + SHARE_SCREENSHOT
                     + SHARE_VIDEO):
            (folder / f"IMG_{index:05d}.MOV").write_bytes(
                synth.build_mov(when.strftime("%Y-%m-%dT%H:%M:%S+0200"),
                                "+44.4268+026.1025/") + body)
            made["video"] += 1
        elif roll < (SHARE_PHOTO_GPS + SHARE_PHOTO_NO_GPS + SHARE_SCREENSHOT
                     + SHARE_VIDEO + SHARE_LIVE):
            place = rng.choice(PLACES)
            (folder / f"IMG_{index:05d}.HEIC").write_bytes(
                synth.build_heic(synth.build_exif(when, place)) + body)
            (folder / f"IMG_{index:05d}.MOV").write_bytes(
                synth.build_mov(when.strftime("%Y-%m-%dT%H:%M:%S+0200"),
                                "+44.4268+026.1025/") + body)
            made["live"] += 1
        else:
            if first_photo is not None:
                shutil.copy2(first_photo, folder / f"IMG_{index:05d}_copy.JPG")
                made["duplicate"] += 1
            else:
                synth.write_photo(folder / f"IMG_{index:05d}.JPG", when, None,
                                  payload=body)
                made["photo"] += 1
    return made


def timed(label: str, fn):
    tracemalloc.start()
    started = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"label": label, "seconds": elapsed, "peak_bytes": peak,
            "result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--payload-kb", type=int, default=4)
    parser.add_argument("--out", default=None,
                        help="working directory (default: a temp folder)")
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the working directory afterwards")
    args = parser.parse_args()

    base = Path(args.out) if args.out else Path("./benchmark-run").resolve()
    if base.exists():
        shutil.rmtree(base)
    source_root, dest = base / "source", base / "backup"
    report = Reporter(quiet=True)
    cfg = Config(dest=dest)

    print(f"Building {args.files} files ({args.payload_kb} KB payload) in {base} ...")
    built = timed("build", lambda: build_library(source_root, args.files,
                                                 args.payload_kb))
    total_bytes = sum(p.stat().st_size for p in source_root.rglob("*") if p.is_file())
    on_disk = sum(1 for p in source_root.rglob("*") if p.is_file())
    print(f"  {on_disk} files, {human_size(total_bytes)}  "
          f"({', '.join(f'{k}={v}' for k, v in built['result'].items())})")

    def run_import(use_cache=True):
        run_cfg = Config(dest=dest, use_source_cache=use_cache)
        with Database(run_cfg.db_path) as db:
            source = FolderSource(source_root, skip_dirs=run_cfg.skip_dirs,
                                  accept_kinds=run_cfg.allowed_kinds())
            importer = pipeline.Importer(run_cfg, db, Geocoder(), report)
            return importer.import_source(source)

    phases = [
        timed("cold import", run_import),
        timed("warm import (source cache)", run_import),
        timed("warm import (--no-cache)", lambda: run_import(use_cache=False)),
        timed("verify --deep", lambda: _verify(cfg, report)),
        timed("regeocode (no-op)", lambda: _regeocode(cfg, report)),
    ]

    print(f"\n{'phase':<28} {'time':>10} {'files/s':>10} {'peak python mem':>18}")
    print("-" * 70)
    for phase in phases:
        rate = on_disk / phase["seconds"] if phase["seconds"] else 0
        print(f"{phase['label']:<28} {human_duration(phase['seconds']):>10} "
              f"{rate:>10.0f} {human_size(phase['peak_bytes']):>18}")

    cold = phases[0]["result"]
    print(f"\ncold import: {cold.imported} copied, {cold.duplicates} duplicates, "
          f"{cold.errors} errors, {human_size(cold.bytes_copied)}")
    with Database(cfg.db_path) as db:
        stats = db.stats()
    print(f"index: {stats['n']} rows, {stats['screenshots'] or 0} screenshots, "
          f"{stats['with_gps'] or 0} with GPS, "
          f"{len(stats['places'])} distinct places")
    print(f"index file: {human_size(cfg.db_path.stat().st_size)}")

    report.close()
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f"\nkept: {base}")
    return 0


def _verify(cfg, report):
    with Database(cfg.db_path) as db:
        return pipeline.verify_backup(cfg, db, report, deep=True)


def _regeocode(cfg, report):
    with Database(cfg.db_path) as db:
        return pipeline.regeocode(cfg, db, Geocoder(), report)


if __name__ == "__main__":
    raise SystemExit(main())
