"""Read-only inspection of an existing photo backup folder of unknown origin.

Walks the folder, summarises what is in it, and dumps whatever metadata the
tool that made it left behind (SQLite indexes, logs). Nothing is ever written
inside the inspected folder: SQLite files are read from a temporary copy and
the report goes next to this script.

    python tools/inspect_backup.py D:\\poze\\backup_final

Writes inspect-<folder name>-<YYYYMMDD-HHMMSS>.log and prints its path.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibackup.mediameta import PHOTO_EXT, SIDECAR_EXT, VIDEO_EXT  # noqa: E402
from ibackup.util import human_size                              # noqa: E402

SAMPLE_PATHS = 20
TREE_DEPTH = 4
TREE_LINE_CAP = 400
NAME_CAP = 60
META_DIRS = (".backup", ".ibackup", ".photobackup")
META_FILE_CAP = 2000
SAMPLE_ROWS = 5
VALUE_CAP = 120
LOG_LINE_CAP = 300
LOG_EDGE = 30
JUNK_NAMES = {"thumbs.db", "desktop.ini"}
TEMP_SUFFIXES = (".part", ".tmp", "~")
SQLITE_MAGIC = b"SQLite format 3\x00"


def fs_path(path: str) -> str:
    """Extended-length form on Windows so paths over 260 characters work."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def plain(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return f"(bad time {ts!r})"


def classify(ext: str) -> str:
    kinds = []
    if ext in PHOTO_EXT:
        kinds.append("photo")
    if ext in VIDEO_EXT:
        kinds.append("video")
    if ext in SIDECAR_EXT:
        kinds.append("sidecar")
    return ",".join(kinds) if kinds else "-"


class Scan:
    def __init__(self):
        self.errors: list[tuple[str, str]] = []
        self.files = 0
        self.bytes = 0
        self.zero = [0, []]
        self.temp = [0, []]
        self.hidden = 0
        self.exts: dict[str, list[int]] = {}
        # relative-parts tuple -> [direct files, files below incl. direct]
        self.folders: dict[tuple, list[int]] = {}
        self.max_depth = 0

    def error(self, path: str, exc: BaseException) -> None:
        self.errors.append((plain(path), f"{type(exc).__name__}: {exc}"))


def scan_tree(root: str) -> Scan:
    scan = Scan()
    root_fs = fs_path(root)

    def onerror(exc: OSError) -> None:
        scan.error(getattr(exc, "filename", None) or root, exc)

    for dirpath, dirnames, filenames in os.walk(root_fs, onerror=onerror):
        try:
            rel = os.path.relpath(dirpath, root_fs)
        except ValueError as exc:
            scan.error(dirpath, exc)
            continue
        parts = () if rel == "." else tuple(rel.split(os.sep))
        depth = len(parts)
        scan.max_depth = max(scan.max_depth, depth)
        if depth <= TREE_DEPTH:
            scan.folders.setdefault(parts, [0, 0])[0] += len(filenames)
        for cut in range(min(depth, TREE_DEPTH) + 1):
            scan.folders.setdefault(parts[:cut], [0, 0])[1] += len(filenames)

        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.stat(full).st_size
            except OSError as exc:
                scan.error(full, exc)
                continue
            scan.files += 1
            scan.bytes += size
            low = name.lower()
            ext = os.path.splitext(low)[1] or "(none)"
            entry = scan.exts.setdefault(ext, [0, 0])
            entry[0] += 1
            entry[1] += size
            if size == 0:
                scan.zero[0] += 1
                if len(scan.zero[1]) < SAMPLE_PATHS:
                    scan.zero[1].append(plain(full))
            if low.endswith(TEMP_SUFFIXES):
                scan.temp[0] += 1
                if len(scan.temp[1]) < SAMPLE_PATHS:
                    scan.temp[1].append(plain(full))
            if name.startswith(".") or low in JUNK_NAMES:
                scan.hidden += 1
    return scan


def clip(value, limit: int = VALUE_CAP) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit})"


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def open_copy(path: str, scratch: str):
    """Open a copy of the database and its -wal file.

    Even a read-only SQLite reader writes to the -shm file of a WAL database,
    so the original is never opened in place.
    """
    notes = []
    copy = os.path.join(scratch, "copy.db")
    try:
        shutil.copy2(fs_path(path), copy)
        if os.path.exists(fs_path(path + "-wal")):
            shutil.copy2(fs_path(path + "-wal"), copy + "-wal")
            notes.append("its -wal file was copied too")
        conn = sqlite3.connect(copy)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, notes
    except (OSError, sqlite3.Error) as exc:
        notes.append(f"could not open a copy: {type(exc).__name__}: {exc}")
        return None, notes


def is_sqlite(path: str) -> bool:
    try:
        with open(fs_path(path), "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def dump_sqlite(path: str, out: list, scan: Scan) -> None:
    out.append(f"--- {path}  (read from a temporary copy)")
    with tempfile.TemporaryDirectory() as scratch:
        conn, notes = open_copy(path, scratch)
        for note in notes:
            out.append(f"  note: {note}")
        if conn is None:
            scan.error(path, RuntimeError("could not open as SQLite (see report)"))
            return
        _dump_tables(conn, path, out, scan)


def _dump_tables(conn, path: str, out: list, scan: Scan) -> None:
    try:
        objects = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [o for o in objects if o[0] == "table"]
        out.append(f"  tables: {', '.join(t[1] for t in tables) or '(none)'}")
        for kind, name, tbl, sql in objects:
            if kind != "table":
                out.append(f"  {kind} {name} on {tbl}: {clip(sql or '', 400)}")
        for _, name, _, sql in tables:
            out.append("")
            out.append(f"  [table] {name}")
            out.append(f"  CREATE: {clip(sql or '', 2000)}")
            try:
                count = conn.execute(f"SELECT count(*) FROM {quote(name)}").fetchone()[0]
                out.append(f"  rows: {count}")
                cur = conn.execute(f"SELECT * FROM {quote(name)} LIMIT {SAMPLE_ROWS}")
                cols = [c[0] for c in cur.description]
                out.append(f"  columns: {', '.join(cols)}")
                for n, row in enumerate(cur.fetchall(), 1):
                    out.append(f"  sample {n}: " + " | ".join(clip(v) for v in row))
            except sqlite3.Error as exc:
                out.append(f"  error: {exc}")
                scan.error(f"{path} [{name}]", exc)
    except sqlite3.Error as exc:
        out.append(f"  error: {exc}")
        scan.error(path, exc)
    finally:
        conn.close()


def read_edges(path: str):
    head, tail = [], deque(maxlen=LOG_EDGE)
    total = 0
    with open(fs_path(path), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            line = line if len(line) <= LOG_LINE_CAP else line[:LOG_LINE_CAP] + "..."
            total += 1
            if total <= LOG_EDGE:
                head.append(line)
            tail.append(line)
    return head, list(tail), total


def find_meta_files(root: str, scan: Scan):
    """Return (meta folders found, [(full path, size, mtime)] below them)."""
    found, files = [], []
    try:
        top = os.listdir(fs_path(root))
    except OSError as exc:
        scan.error(root, exc)
        return found, files

    def onerror(exc: OSError) -> None:
        scan.error(getattr(exc, "filename", None) or root, exc)

    for name in sorted(top):
        if name.lower() not in META_DIRS:
            continue
        base = os.path.join(root, name)
        if not os.path.isdir(fs_path(base)):
            continue
        found.append(base)
        for dirpath, _dirs, names in os.walk(fs_path(base), onerror=onerror):
            for fname in names:
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError as exc:
                    scan.error(full, exc)
                    continue
                files.append((plain(full), st.st_size, st.st_mtime))
    files.sort()
    return found, files


def section(out: list, number: int, title: str) -> None:
    out.append("")
    out.append("=" * 78)
    out.append(f"{number}. {title}")
    out.append("=" * 78)


def sample_list(out: list, label: str, bucket: list) -> None:
    out.append(f"{label}: {bucket[0]}")
    for path in bucket[1]:
        out.append(f"    {path}")
    if bucket[0] > len(bucket[1]):
        out.append(f"    ... and {bucket[0] - len(bucket[1])} more")


def report_header(out: list, root: str, started: datetime) -> None:
    out.append("1. HEADER")
    out.append(f"script:  {Path(__file__).name}")
    out.append(f"time:    {started.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"python:  {sys.version.split()[0]} ({platform.python_implementation()})")
    out.append(f"os:      {platform.platform()}")
    out.append(f"target:  {root}")
    try:
        usage = shutil.disk_usage(root)
        out.append(f"drive:   total {human_size(usage.total)} ({usage.total} bytes), "
                   f"free {human_size(usage.free)} ({usage.free} bytes)")
    except OSError as exc:
        out.append(f"drive:   disk_usage failed: {exc}")


def report_layout(out: list, scan: Scan) -> None:
    folders = scan.folders
    out.append(f"deepest folder level seen: {scan.max_depth} "
               f"(tree below shows levels 1-{TREE_DEPTH}; deeper files count in their level-{TREE_DEPTH} ancestor)")
    out.append("")
    out.append("tree: <name>  direct=<files in the folder>  total=<files below, incl. direct>")
    ordered = sorted(scan.folders, key=lambda p: [s.lower() for s in p])
    shown = 0
    for parts in ordered:
        if shown >= TREE_LINE_CAP:
            break
        direct, total = folders[parts]
        label = "(root)" if not parts else parts[-1]
        out.append(f"{'  ' * len(parts)}{label}  direct={direct}  total={total}")
        shown += 1
    if len(ordered) > shown:
        out.append(f"... {len(ordered) - shown} more folder lines omitted (cap {TREE_LINE_CAP})")

    for level in (1, 2, 3):
        names = Counter(p[-1] for p in folders if len(p) == level)
        out.append("")
        out.append(f"distinct depth-{level} folder names: {len(names)} "
                   f"(name x number of folders with that name)")
        for name, n in sorted(names.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:NAME_CAP]:
            out.append(f"  {n:>6} x {name}")
        if len(names) > NAME_CAP:
            out.append(f"  ... {len(names) - NAME_CAP} more names omitted")


def report_meta(out: list, root: str, scan: Scan) -> None:
    found, files = find_meta_files(root, scan)
    if not found:
        out.append(f"none of {', '.join(META_DIRS)} present at the top level")
        section(out, 6, "SQLITE FILES IN META FOLDERS (read-only)")
        out.append("n/a")
        section(out, 7, "LOGS IN META FOLDERS")
        out.append("n/a")
        return
    out.append("folders: " + ", ".join(found))
    out.append(f"files: {len(files)}")
    for path, size, mtime in files[:META_FILE_CAP]:
        out.append(f"  {size:>14}  {fmt_time(mtime)}  {path}")
    if len(files) > META_FILE_CAP:
        out.append(f"  ... {len(files) - META_FILE_CAP} more omitted")

    section(out, 6, "SQLITE FILES IN META FOLDERS (read-only)")
    dbs = [f for f in files if is_sqlite(f[0])]
    if not dbs:
        out.append("no SQLite files found")
    for path, _size, _mtime in dbs:
        dump_sqlite(path, out, scan)
        out.append("")

    section(out, 7, "LOGS IN META FOLDERS")
    logs = [f for f in files
            if f[0].lower().endswith((".log", ".txt"))
            or f"{os.sep}logs{os.sep}" in f[0].lower()]
    logs = [f for f in logs if not is_sqlite(f[0])]
    if not logs:
        out.append("no log files found")
    for path, size, mtime in logs[:META_FILE_CAP]:
        out.append(f"  {size:>14}  {fmt_time(mtime)}  {path}")
    for path, _size, mtime in sorted(logs, key=lambda f: f[2], reverse=True)[:3]:
        out.append("")
        out.append(f"--- {path}  (modified {fmt_time(mtime)})")
        try:
            head, tail, total = read_edges(path)
        except OSError as exc:
            out.append(f"  error: {exc}")
            scan.error(path, exc)
            continue
        out.append(f"  {total} lines; first {len(head)}:")
        out.extend("  | " + line for line in head)
        rest = tail[-(total - len(head)):] if total > len(head) else []
        if rest:
            skipped = total - len(head) - len(rest)
            out.append(f"  last {len(rest)}" + (f" ({skipped} lines skipped):" if skipped else ":"))
            out.extend("  | " + line for line in rest)


def build_report(root: str, started: datetime) -> tuple[list, Scan]:
    out: list[str] = []
    report_header(out, root, started)
    scan = scan_tree(root)

    section(out, 2, "TOTALS")
    out.append(f"files: {scan.files}")
    out.append(f"total size: {human_size(scan.bytes)} ({scan.bytes} bytes)")
    sample_list(out, "0-byte files", scan.zero)
    sample_list(out, "files ending in .part / .tmp / ~", scan.temp)
    out.append(f"hidden-style files (name starts with '.', Thumbs.db, desktop.ini): {scan.hidden}")

    section(out, 3, "EXTENSIONS")
    out.append(f"distinct extensions: {len(scan.exts)}   (kind: media types per ibackup.mediameta)")
    out.append(f"  {'ext':<12} {'count':>9} {'size':>12}  kind")
    for ext, (count, size) in sorted(scan.exts.items(), key=lambda kv: (-kv[1][0], kv[0])):
        out.append(f"  {ext:<12} {count:>9} {human_size(size):>12}  {classify(ext)}")

    section(out, 4, "LAYOUT")
    report_layout(out, scan)

    section(out, 5, "META FOLDERS (.backup / .ibackup / .photobackup)")
    report_meta(out, root, scan)

    section(out, 8, "ERRORS WHILE WALKING")
    out.append(f"errors: {len(scan.errors)}")
    for path, message in scan.errors:
        out.append(f"  {path}: {message}")
    return out, scan


def main(argv=None, out_dir=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only inspection of a backup folder.")
    parser.add_argument("folder", help="the backup folder to inspect")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.folder)
    if not os.path.isdir(fs_path(root)):
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    out_dir = Path(out_dir) if out_dir else Path(__file__).resolve().parent
    out_dir = out_dir.resolve()
    real_root = Path(root).resolve()
    if out_dir == real_root or real_root in out_dir.parents:
        print(f"refusing to write the report inside the inspected folder: {out_dir}", file=sys.stderr)
        return 2

    started = datetime.now()
    name = os.path.basename(root.rstrip("\\/")) or os.path.splitdrive(root)[0].rstrip(":\\/") or "root"
    name = "".join(c if c not in '<>:"/\\|?*' else "_" for c in name)
    report_path = out_dir / f"inspect-{name}-{started.strftime('%Y%m%d-%H%M%S')}.log"

    lines, scan = build_report(root, started)
    lines.append("")
    lines.append(f"finished in {(datetime.now() - started).total_seconds():.1f}s")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    summary = (f"{scan.files} files, {human_size(scan.bytes)}, {len(scan.errors)} errors")
    for text in (str(report_path), summary):
        print(text.encode(sys.stdout.encoding or "utf-8", "backslashreplace")
              .decode(sys.stdout.encoding or "utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
