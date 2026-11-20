"""Read-only measurements on a backup before HEIC photos are converted into it.

1. JPEG settings of the JPGs in the backup: quality, chroma subsampling, ICC
   profile, and whether EXIF date and GPS survived. JPGs whose name stem matches
   a HEIC source in the old index (.ibackup\\index.db) are reported apart: they
   are most likely the old tool's HEIC -> JPG conversions.
2. For every HEIC in a second folder (--heic): is there a JPG in the backup with
   the same name stem and the same EXIF date (its converted copy)? For an iPhone
   edit (IMG_E1234), where is the original IMG_1234?

Nothing is ever written inside the measured folders: files are only opened for
reading, the index is read from a temporary copy, the report goes next to this
script.

    python tools/measure_final.py D:\\poze\\backup_final --heic D:\\salvare-backup

Writes measure-<name>-<YYYYMMDD-HHMMSS>.log and prints its path. Ctrl+C or an
unexpected error writes a report marked PARTIAL and exits 130.
"""
from __future__ import annotations

import sys

# A repo copy sitting inside a measured folder must not get __pycache__ written into it.
sys.dont_write_bytecode = True

import argparse
import os
import platform
import re
import sqlite3
import struct
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_backup import fs_path, open_copy, plain       # noqa: E402
from verify_backup import inside, safe_name, section, write_text  # noqa: E402
from ibackup import exif, isobmff                            # noqa: E402
from ibackup.mediameta import JPEG_HEAD, extract             # noqa: E402
from ibackup.util import human_size                          # noqa: E402

META_DIR = ".ibackup"
JPEG_EXT = {".jpg", ".jpeg", ".jpe"}
HEIC_EXT = {".heic", ".heif"}
LIST_CAP = 2000
WHERE_CAP = 5
TOP = 10
PROGRESS_SECONDS = 2.0
EDIT_NAME = re.compile(r"^(IMG_)E(\d+)$", re.IGNORECASE)
# Copy suffixes added on a name collision: IMG_1234_2 (this tool), "IMG_1234 (1)" (Windows).
COPY_SUFFIX = re.compile(r"(?:_\d{1,2}| \(\d+\))$")

# ITU T.81 Annex K.1 luminance table, natural (row-major) order.
STD_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
# DQT stores the 64 values in zigzag order; ZIGZAG[k] is the natural index of the k-th.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def say(text: str) -> None:
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, "backslashreplace").decode(enc), flush=True)


def ijg_table(quality: int) -> tuple:
    """The luminance table libjpeg (and so Pillow) writes for a quality setting."""
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    return tuple(min(255, max(1, (v * scale + 50) // 100)) for v in STD_LUMA)


IJG_TABLES = {ijg_table(q): q for q in range(100, 0, -1)}


def quality_label(luma) -> str:
    if luma is None:
        return "no DQT found"
    q = IJG_TABLES.get(tuple(luma))
    if q is not None:
        return f"q{q} (IJG table, exact)"
    scale = 100.0 * sum(luma) / sum(STD_LUMA)
    est = (200 - scale) / 2 if scale <= 100 else 5000 / scale
    return f"~q{round(est)} (other encoder, estimate)"


def subsampling(comps) -> str:
    if not comps:
        return "no SOF found"
    if len(comps) == 1:
        return "grayscale"
    yh, yv = comps[0] >> 4, comps[0] & 15
    ch, cv = comps[1] >> 4, comps[1] & 15
    known = {(2, 2, 1, 1): "4:2:0", (2, 1, 1, 1): "4:2:2", (1, 1, 1, 1): "4:4:4"}
    return known.get((yh, yv, ch, cv), f"{yh}x{yv}/{ch}x{cv}")


def jpeg_segments(data: bytes) -> dict:
    """Luminance quant table (natural order), SOF sampling factors, ICC profile
    presence and the EXIF block, read from the segments before the scan data."""
    info = {"luma": None, "comps": None, "icc": False, "exif": None, "size": None}
    if data[:2] != b"\xff\xd8":
        return info
    pos, n = 2, len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xDA, 0xD9):
            break
        (seg_len,) = struct.unpack_from(">H", data, pos + 2)
        if seg_len < 2:
            break
        start, end = pos + 4, min(pos + 2 + seg_len, n)
        seg = data[start:end]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00" and info["exif"] is None:
            info["exif"] = seg[6:]
        elif marker == 0xE2 and seg[:12] == b"ICC_PROFILE\x00":
            info["icc"] = True
        elif marker == 0xDB:
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                width = 128 if pq else 64
                raw = seg[i + 1:i + 1 + width]
                if len(raw) < width:
                    break
                vals = (struct.unpack(">64H", raw) if pq else tuple(raw))
                if tq == 0 and info["luma"] is None:
                    natural = [0] * 64
                    for k, v in enumerate(vals):
                        natural[ZIGZAG[k]] = v
                    info["luma"] = tuple(natural)
                i += 1 + width
        elif marker in SOF_MARKERS and info["comps"] is None and len(seg) >= 6:
            height, width_px = struct.unpack_from(">HH", seg, 1)
            ncomp = seg[5]
            info["size"] = (width_px, height)
            info["comps"] = tuple(seg[6 + 3 * c + 1] for c in range(ncomp)
                                  if 6 + 3 * c + 1 < len(seg))
        pos = start + seg_len - 2
    return info


class Group:
    def __init__(self, title: str):
        self.title = title
        self.files = 0
        self.quality: Counter = Counter()
        self.example: dict = {}
        self.sampling: Counter = Counter()
        self.icc = 0
        self.exif = 0
        self.date_source: Counter = Counter()
        self.gps = 0
        self.software: Counter = Counter()
        self.dims: Counter = Counter()

    def add(self, rel: str, rec: tuple) -> None:
        label, sampling, icc, has_exif, date_source, has_gps, software, dims = rec
        self.files += 1
        self.quality[label] += 1
        self.example.setdefault(label, rel)
        self.sampling[sampling] += 1
        self.icc += icc
        self.exif += has_exif
        self.date_source[date_source or "(no EXIF date)"] += 1
        self.gps += has_gps
        self.software[software or "(none)"] += 1
        if dims:
            self.dims[dims] += 1

    def report(self, out: list) -> None:
        out.append(f"--- {self.title}: {self.files} files")
        if not self.files:
            return
        out.append("quality (luminance table):")
        for label, n in self.quality.most_common(TOP):
            out.append(f"  {n:>7}  {label}   e.g. {self.example[label]}")
        if len(self.quality) > TOP:
            out.append(f"  ... {len(self.quality) - TOP} more values")
        out.append("chroma subsampling: " + ", ".join(f"{k} {v}" for k, v in self.sampling.most_common()))
        out.append(f"ICC colour profile: {self.icc} of {self.files}")
        out.append(f"EXIF block: {self.exif} of {self.files}")
        out.append("EXIF date: " + ", ".join(f"{k} {v}" for k, v in self.date_source.most_common()))
        out.append(f"GPS: {self.gps} of {self.files}")
        out.append("Software tag: " + ", ".join(f"{k!r} {v}" for k, v in self.software.most_common(TOP)))
        out.append("pixel size: " + ", ".join(f"{k} {v}" for k, v in self.dims.most_common(5)))
        out.append("")


def index_heic_stems(root: str, notes: list) -> set | None:
    path = os.path.join(root, META_DIR, "index.db")
    if not os.path.isfile(fs_path(path)):
        notes.append(f"no old index at {path}; JPGs are not split into likely conversions")
        return None
    stems: set = set()
    with tempfile.TemporaryDirectory() as scratch:
        conn, open_notes = open_copy(path, scratch)
        notes.extend(open_notes)
        if conn is None:
            notes.append("old index unreadable; JPGs are not split into likely conversions")
            return None
        try:
            rows = 0
            for (src,) in conn.execute("SELECT path FROM media_index"):
                rows += 1
                name = re.split(r"[\\/]", src or "")[-1]
                stem, ext = os.path.splitext(name)
                if ext.lower() in HEIC_EXT:
                    stems.add(stem.lower())
            notes.append(f"old index {path}: {rows} rows, {len(stems)} distinct HEIC name stems")
        except sqlite3.Error as exc:
            notes.append(f"cannot read media_index: {exc}; JPGs are not split")
            return None
        finally:
            conn.close()
    return stems


class Progress:
    def __init__(self, what: str):
        self.what = what
        self.count = 0
        self.last = time.monotonic()

    def tick(self) -> None:
        self.count += 1
        now = time.monotonic()
        if now - self.last >= PROGRESS_SECONDS:
            self.last = now
            say(f"  {self.what}: {self.count} files read")


def walk(root: str, exts: set, errors: list, skip_meta: bool):
    def onerror(exc: OSError) -> None:
        errors.append((plain(getattr(exc, "filename", None) or root), f"{type(exc).__name__}: {exc}"))

    root_fs = fs_path(root)
    for dirpath, dirnames, filenames in os.walk(root_fs, onerror=onerror):
        if skip_meta and os.path.normcase(dirpath) == os.path.normcase(root_fs):
            dirnames[:] = [d for d in dirnames if d.lower() != META_DIR]
        dirnames.sort(key=str.lower)
        for name in sorted(filenames, key=str.lower):
            if os.path.splitext(name)[1].lower() in exts:
                full = os.path.join(dirpath, name)
                yield full, os.path.relpath(full, root_fs)


def make_groups(split: bool) -> dict:
    groups = {}
    if split:
        groups["stem"] = Group("JPGs whose name stem is a HEIC source in the old index (likely "
                               "conversions; may include native JPGs that reuse an IMG_nnnn name)")
    groups["apple"] = Group(("other " if split else "") + "JPGs with EXIF Make 'Apple' (iPhone photos: "
                            "native JPGs, or conversions the old index does not name)")
    groups["rest"] = Group("all other JPGs")
    return groups


def base_stem(stem: str) -> str:
    return COPY_SUFFIX.sub("", stem)


def measure_jpegs(root: str, heic_stems, errors: list, keys: dict, loose: dict, stems: dict,
                  groups: dict, records: dict) -> None:
    progress = Progress("backup JPGs")
    for full, rel in walk(root, JPEG_EXT, errors, skip_meta=True):
        try:
            with open(full, "rb") as fh:
                data = fh.read(JPEG_HEAD)
        except OSError as exc:
            errors.append((plain(full), f"{type(exc).__name__}: {exc}"))
            continue
        progress.tick()
        info = jpeg_segments(data)
        if data[:2] != b"\xff\xd8":
            errors.append((plain(full), f"not a JPEG (first bytes {data[:4]!r}, {len(data)} bytes read)"))
        elif info["luma"] is None:
            errors.append((plain(full), f"no quantization table in the first {len(data)} bytes"))
        taken, source, has_gps, make, software = None, "", False, None, None
        if info["exif"]:
            try:
                tags = exif.parse_tiff(info["exif"])
                taken, source = exif.exif_datetime(tags)
                has_gps = exif.exif_gps(tags) is not None
                make, _model = exif.exif_camera(tags)
                software = exif.exif_software(tags)
            except Exception as exc:
                errors.append((plain(full), f"EXIF unreadable: {type(exc).__name__}: {exc}"))
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        stems.setdefault(stem, []).append((rel, taken))
        if taken is not None:
            keys.setdefault((stem, taken), []).append(rel)
            loose.setdefault((base_stem(stem), taken), []).append(rel)
        rec = (quality_label(info["luma"]), subsampling(info["comps"]), info["icc"],
               info["exif"] is not None, source, has_gps, software,
               "%dx%d" % info["size"] if info["size"] else None)
        records[rel] = rec
        if heic_stems is not None and stem in heic_stems:
            key = "stem"
        elif (make or "").strip().lower().startswith("apple"):
            key = "apple"
        else:
            key = "rest"
        groups[key].add(rel, rec)


def stem_candidates(stems: dict, stem: str) -> list:
    rows = stems.get(stem, [])
    shown = [f"{r} (EXIF date {t or 'none'})" for r, t in rows[:WHERE_CAP]]
    if len(rows) > WHERE_CAP:
        shown.append(f"... {len(rows) - WHERE_CAP} more JPGs named {stem}")
    return shown


def heic_problem(full: str, size: int) -> str:
    try:
        with open(full, "rb") as fh:
            if isobmff.container_kind(fh, size) != "heif":
                return "not a HEIF container (damaged, empty or another format)"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return "no EXIF date in the HEIC (no Exif item, or no DateTimeOriginal/Digitized/DateTime)"


def match_heics(folder: str, errors: list, keys: dict, loose: dict, stems: dict, found: dict) -> None:
    progress = Progress("HEIC files")
    for full, rel in walk(folder, HEIC_EXT, errors, skip_meta=False):
        try:
            size = os.stat(full).st_size
        except OSError as exc:
            errors.append((plain(full), f"{type(exc).__name__}: {exc}"))
            continue
        progress.tick()
        meta = extract(Path(full), use_filename=False, use_mtime=False, detect_screenshots=False)
        if meta.read_error:
            errors.append((plain(full), f"metadata: {meta.read_error}"))
        # Same date sources as the JPG side: the GPS clock fallback is not an EXIF date.
        taken = meta.taken_at if meta.taken_at_source.startswith("exif:Date") else None
        if taken is None and not meta.read_error:
            errors.append((plain(full), heic_problem(full, size)))
        stem = os.path.splitext(os.path.basename(rel))[0]
        low = stem.lower()
        row = {"rel": rel, "size": size, "taken": taken, "edit": bool(EDIT_NAME.match(stem)),
               "where": []}
        own = keys.get((low, taken), []) if taken is not None else []
        if len(own) == 1:
            row["where"] = own
            found["converted"].append(row)
            continue
        if len(own) > 1:
            row["where"] = own
            found["ambiguous"].append(row)
            continue
        edit = EDIT_NAME.match(stem)
        original_stem = (edit.group(1) + edit.group(2)).lower() if edit else None
        if edit and taken is not None:
            original = keys.get((original_stem, taken), [])
            if original:
                row["where"] = original
                found["edit_original"].append(row)
                continue
        near = loose.get((base_stem(low), taken), []) if taken is not None else []
        if near:
            row["where"] = near
            found["suffixed"].append(row)
            continue
        if low in stems or original_stem in stems:
            row["where"] = stem_candidates(stems, low) + (
                stem_candidates(stems, original_stem) if original_stem else [])
            found["stem_only"].append(row)
            continue
        found["none"].append(row)


def list_rows(out: list, rows: list, show_where: bool) -> None:
    for row in rows[:LIST_CAP]:
        line = f"  {row['size']:>12}  {row['taken'] or 'no EXIF date'}  {row['rel']}"
        out.append(line)
        if show_where:
            for where in row["where"]:
                out.append(f"      -> {where}")
    if len(rows) > LIST_CAP:
        out.append(f"  ... {len(rows) - LIST_CAP} more")


def heic_report(out: list, folder: str, found: dict) -> None:
    labels = (
        ("converted", "a JPG with the same name stem and the same EXIF date is in the backup "
                      "(the same photo, most likely its converted copy)"),
        ("ambiguous", "more than one JPG has the same name stem and EXIF date"),
        ("edit_original", "an edit (IMG_E...) whose original IMG_... JPG with the same EXIF date "
                          "is in the backup"),
        ("suffixed", "a JPG with the same EXIF date whose name differs only by a copy suffix "
                     "(IMG_1234_2, 'IMG_1234 (1)')"),
        ("stem_only", "a JPG with the same name stem (for an edit: or its original's) is in the "
                      "backup, but not with the same EXIF date (or the HEIC has none)"),
        ("none", "no JPG with this name stem in the backup"),
    )
    total = sum(len(v) for v in found.values())
    out.append(f"folder: {folder}")
    out.append(f"HEIC files: {total}   ({human_size(sum(r['size'] for v in found.values() for r in v))})")
    for key, text in labels:
        rows = found[key]
        edits = sum(r["edit"] for r in rows)
        out.append(f"  {len(rows):>6}  {text}   [of which IMG_E edits: {edits}; "
                   f"{human_size(sum(r['size'] for r in rows))}]")
    for key, text in labels[1:]:
        out.append("")
        out.append(f"--- {key}: {text}")
        list_rows(out, found[key], show_where=key != "none")
    out.append("")
    out.append("--- converted: first 20, as a sample")
    list_rows(out, found["converted"][:20], show_where=True)


def header(out: list, started: datetime, partial: str | None) -> None:
    if partial:
        out.append(f"*** PARTIAL REPORT: {partial} ***")
        out.append("")
    out.append("1. HEADER")
    out.append(f"script:  {Path(__file__).name}")
    out.append(f"time:    {started.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"python:  {sys.version.split()[0]} ({platform.python_implementation()})")
    out.append(f"os:      {platform.platform()}")


def main(argv=None, out_dir=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only measurements before converting HEIC into a backup.")
    parser.add_argument("backup", help="the backup folder (e.g. D:\\poze\\backup_final)")
    parser.add_argument("--heic", help="a folder whose HEIC files are matched to the backup's JPGs")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.backup)
    folders = [root] + ([os.path.abspath(args.heic)] if args.heic else [])
    for folder in folders:
        if not os.path.isdir(fs_path(folder)):
            print(f"not a folder: {folder}", file=sys.stderr)
            return 2
    out_path = (Path(out_dir) if out_dir else Path(__file__).resolve().parent).resolve()
    for folder in folders:
        if inside(out_path, Path(folder).resolve()):
            print(f"refusing to write the report inside a measured folder: {out_path}", file=sys.stderr)
            return 2

    started = datetime.now()
    t0 = time.monotonic()
    errors: list = []
    notes: list = []
    keys: dict = {}
    loose: dict = {}
    stems: dict = {}
    records: dict = {}
    groups: dict = {}
    found = None
    partial = None
    try:
        heic_stems = index_heic_stems(root, notes)
        groups = make_groups(heic_stems is not None)
        say(f"reading JPG headers in {root} ...")
        measure_jpegs(root, heic_stems, errors, keys, loose, stems, groups, records)
        if args.heic:
            say(f"reading HEIC files in {folders[1]} ...")
            found = {k: [] for k in ("converted", "ambiguous", "edit_original", "suffixed",
                                     "stem_only", "none")}
            match_heics(folders[1], errors, keys, loose, stems, found)
    except KeyboardInterrupt:
        partial = ("interrupted by Ctrl+C; the numbers below cover only what was read before"
                   + (" (the HEIC phase had started)" if found is not None else ""))
    except Exception as exc:
        partial = (f"stopped by an error ({type(exc).__name__}: {exc}); the numbers below cover "
                   f"only what was read before" + (" (the HEIC phase had started)" if found is not None else ""))
    groups = list(groups.values())
    if found is not None:
        paired = Group("JPGs matched to a HEIC in --heic by name stem and EXIF date (the same "
                       "photo; IJG quality = encoded by libjpeg/Pillow, so by the old tool)")
        for rel in sorted({row["where"][0] for row in found["converted"]}):
            paired.add(rel, records[rel])
        groups.append(paired)

    out: list[str] = []
    header(out, started, partial)
    out.append(f"backup:  {root}")
    out.append(f"heic:    {folders[1] if args.heic else '(not given)'}")
    for note in notes:
        out.append(f"note:    {note}")
    section(out, 2, "JPEG SETTINGS IN THE BACKUP")
    out.append(f"JPGs read: {len(records)}   (first {JPEG_HEAD // 1024} KB of each)")
    out.append("")
    for group in groups:
        group.report(out)
    section(out, 3, "HEIC FILES MATCHED TO THE BACKUP'S JPGs")
    if found is None:
        out.append("not run" + ("" if args.heic else " (no --heic folder given)"))
    else:
        heic_report(out, folders[1], found)
    section(out, 4, "ERRORS")
    out.append(f"errors: {len(errors)}")
    for path, message in errors[:LIST_CAP]:
        out.append(f"  {path}: {message}")
    if len(errors) > LIST_CAP:
        out.append(f"  ... {len(errors) - LIST_CAP} more")
    out.append("")
    out.append(f"finished in {time.monotonic() - t0:.1f}s")

    report = out_path / f"measure-{safe_name(root)}-{started.strftime('%Y%m%d-%H%M%S')}.log"
    out_path.mkdir(parents=True, exist_ok=True)
    write_text(report, out)
    summary = f"{len(records)} JPGs"
    if found is not None:
        summary += (f", {sum(len(v) for v in found.values())} HEIC "
                    f"({len(found['converted'])} with their JPG, "
                    f"{len(found['edit_original'])} edits with their original)")
    summary += f", {len(errors)} errors" + (" - PARTIAL" if partial else "")
    say(str(report))
    say(summary)
    return 130 if partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
