"""Read-only content verification of a photo backup made by another tool.

The backup's index (.ibackup\\index.db, table media_index) stores a content
hash per file, but its `path` column is the SOURCE path the file was copied
from, so files are matched by hash only. Nothing is ever written inside the
inspected folders: the index is read from a temporary copy and the reports go
next to this script.

Mode 1, hash every file in the backup and compare with the index:

    python tools/verify_backup.py backup D:\\poze\\backup_final

Mode 2, say whether the content of every file in some source folders exists
in the backup (by the index, and by the TSV that mode 1 wrote):

    python tools/verify_backup.py source D:\\poze\\backup_final "D:\\POZE\\_backup23" --hashes <tsv>

Writes verify-backup-<name>-<YYYYMMDD-HHMMSS>.log and .tsv, or
verify-source-<name>-<YYYYMMDD-HHMMSS>.log, and prints the path. Ctrl+C writes
a report marked PARTIAL and exits 130. blake3 is optional (pip install blake3).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_backup import classify, fmt_time, fs_path, open_copy, plain  # noqa: E402
from ibackup.util import human_duration, human_size                       # noqa: E402

CHUNK = 2 * 1024 * 1024
PROGRESS_SECONDS = 2.0
DETECT_MATCHES = 20
DETECT_FILES = 200
DUP_GROUP_CAP = 50
META_DIR = ".ibackup"
ALGOS = ("blake3", "sha256")
OLD_TOOL_SKIPPED = {".aae", ".webp", ".gif", ".avif", ".zip", ".db"}
BUCKETS = (("<10 MB", 10 * 1024 ** 2), ("10-100 MB", 100 * 1024 ** 2),
           ("100 MB-1 GB", 1024 ** 3), (">1 GB", None))
BLAKE3_HINT = "blake3 is not installed; install it with: pip install blake3"


def blake3_available() -> bool:
    try:
        import blake3  # noqa: F401
        return True
    except ImportError:
        return False


def offer_blake3_install() -> bool:
    """Ask before installing; only on an interactive console, never silently."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("blake3 is not installed. Install it now with pip? [y/N] ")
    except EOFError:
        return False
    if answer.strip().lower() not in ("y", "yes", "d", "da"):
        return False
    result = subprocess.run([sys.executable, "-m", "pip", "install", "blake3"])
    importlib.invalidate_caches()
    return result.returncode == 0 and blake3_available()


def make_hasher(algo: str):
    if algo == "blake3":
        import blake3
        return blake3.blake3()
    return hashlib.sha256()


def say(text: str) -> None:
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, "backslashreplace").decode(enc), flush=True)


def ext_of(name: str) -> str:
    return os.path.splitext(name.lower())[1] or "(none)"


def bucket_of(size: int) -> str:
    for label, limit in BUCKETS:
        if limit is None or size < limit:
            return label
    return BUCKETS[-1][0]


# ---------------------------------------------------------------- scanning

class Entry:
    __slots__ = ("full", "rel", "size", "mtime")

    def __init__(self, full, rel, size, mtime):
        self.full, self.rel, self.size, self.mtime = full, rel, size, mtime


def scan_files(root: str, skip_meta: bool, errors: list) -> list[Entry]:
    """Stat-only walk. Never stops on an error; errors are appended."""
    root_fs = fs_path(root)
    entries: list[Entry] = []

    def onerror(exc: OSError) -> None:
        errors.append((plain(getattr(exc, "filename", None) or root),
                       f"{type(exc).__name__}: {exc}"))

    for dirpath, dirnames, filenames in os.walk(root_fs, onerror=onerror):
        if skip_meta and os.path.normcase(dirpath) == os.path.normcase(root_fs):
            dirnames[:] = [d for d in dirnames if d.lower() != META_DIR]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                rel = os.path.relpath(full, root_fs)
            except (OSError, ValueError) as exc:
                errors.append((plain(full), f"{type(exc).__name__}: {exc}"))
                continue
            entries.append(Entry(full, rel, st.st_size, st.st_mtime))
    entries.sort(key=lambda e: e.rel.lower())
    return entries


# ----------------------------------------------------------------- hashing

class Progress:
    def __init__(self, files: int, nbytes: int):
        self.total_files, self.total_bytes = files, nbytes
        self.files = 0
        self.bytes = 0
        self.start = time.monotonic()
        self.last = self.start

    def add(self, n: int) -> None:
        self.bytes += n
        now = time.monotonic()
        if now - self.last >= PROGRESS_SECONDS:
            self.last = now
            self.show(now)

    def show(self, now: float) -> None:
        elapsed = max(now - self.start, 1e-9)
        rate = self.bytes / elapsed
        left = max(self.total_bytes - self.bytes, 0)
        eta = human_duration(left / rate) if rate > 0 else "?"
        say(f"  {self.files}/{self.total_files} files, "
            f"{self.bytes / 1024 ** 3:.2f}/{self.total_bytes / 1024 ** 3:.2f} GB, "
            f"{rate / 1024 ** 2:.1f} MB/s, ETA {eta}")


class Detector:
    """Picks the hash algorithm: all candidates until one is clearly right."""

    def __init__(self, candidates: list[str], known: set, forced: str | None):
        self.candidates = candidates
        self.known = known
        self.matches = {c: 0 for c in candidates}
        self.tried = 0
        self.winner = forced or (candidates[0] if len(candidates) == 1 else None)
        self.detected = self.winner is None
        self._early = [0, 0]      # files checked, matches: for the early console verdict
        self._told = False

    def algos(self) -> list[str]:
        return [self.winner] if self.winner else self.candidates

    def observe(self, digests: dict, size: int) -> None:
        if size == 0:
            return
        if self.winner:
            self._early_check(digests.get(self.winner))
            return
        self.tried += 1
        for algo, digest in digests.items():
            if digest in self.known:
                self.matches[algo] += 1
        if max(self.matches.values()) >= DETECT_MATCHES or self.tried >= DETECT_FILES:
            self.finalize()

    def _early_check(self, digest) -> None:
        if self._told:
            return
        self._early[0] += 1
        self._early[1] += digest in self.known
        if self._early[1] >= DETECT_MATCHES or self._early[0] >= DETECT_FILES:
            self._tell(self._early[1], self._early[0])

    def _tell(self, found: int, tried: int) -> None:
        self._told = True
        say(f"algorithm: {self.winner} ({found} index matches in the first {tried} files)")
        if found == 0:
            say("WARNING: nothing matched the index so far; the rest of the run "
                "will not match either. Press Ctrl+C to stop and send the report.")

    def finalize(self) -> None:
        if not self.winner:
            self.winner = max(self.candidates, key=lambda c: self.matches[c])
            self._tell(self.matches[self.winner], self.tried)


def hash_file(path: str, algos: list[str], progress: Progress) -> dict:
    hashers = {a: make_hasher(a) for a in algos}
    with open(fs_path(path), "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            for h in hashers.values():
                h.update(chunk)
            progress.add(len(chunk))
    return {a: h.hexdigest() for a, h in hashers.items()}


def run_hashing(entries, detector: Detector, progress: Progress, sink, errors: list) -> bool:
    """Hash every entry; sink(entry, digest) gets the winning algorithm's digest.

    Returns True when interrupted by Ctrl+C. During detection results wait in
    `pending` until a winner is known.
    """
    pending: list = []

    def flush() -> None:
        if detector.winner:
            for e, digests in pending:
                sink(e, digests[detector.winner])
            pending.clear()

    try:
        for entry in entries:
            before = progress.bytes
            try:
                digests = hash_file(entry.full, detector.algos(), progress)
            except OSError as exc:
                progress.add(max(0, entry.size - (progress.bytes - before)))
                errors.append((plain(entry.full), f"{type(exc).__name__}: {exc}"))
                progress.files += 1
                continue
            progress.files += 1
            detector.observe(digests, entry.size)
            pending.append((entry, digests))
            flush()
    except KeyboardInterrupt:
        detector.finalize()
        flush()
        return True
    detector.finalize()
    flush()
    return False


# ------------------------------------------------------------------- index

class Index:
    def __init__(self):
        self.rows = 0
        self.by_hash: dict[str, list] = {}     # hash -> [(source path, size)]
        self.sizes: Counter = Counter()
        self.lengths: Counter = Counter()
        self.path = ""

    @property
    def hashes(self) -> set:
        return set(self.by_hash)


def load_index(root: str) -> Index:
    idx = Index()
    idx.path = os.path.join(root, META_DIR, "index.db")
    if not os.path.isfile(fs_path(idx.path)):
        raise RuntimeError(f"no index at {idx.path}")
    with tempfile.TemporaryDirectory() as scratch:
        conn, notes = open_copy(idx.path, scratch)
        if conn is None:
            raise RuntimeError("; ".join(notes))
        try:
            for path, size, digest in conn.execute(
                    "SELECT path, size_bytes, hash FROM media_index"):
                idx.rows += 1
                digest = (digest or "").strip().lower()
                idx.lengths[len(digest)] += 1
                if digest:
                    idx.by_hash.setdefault(digest, []).append((path, size))
                if size is not None:
                    idx.sizes[size] += 1
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot read media_index: {exc}") from exc
        finally:
            conn.close()
    return idx


def load_tsv(path: str) -> tuple[str | None, set]:
    algo, hashes = None, set()
    with open(fs_path(path), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith("#algo="):
                algo = line[6:].strip()
            elif line.startswith("#") or line.startswith("relpath\t"):
                continue
            else:
                cols = line.split("\t")
                if len(cols) >= 4 and cols[3]:
                    hashes.add(cols[3].lower())
    return algo, hashes


# ------------------------------------------------------------------ report

def section(out: list, number: int, title: str) -> None:
    out.append("")
    out.append("=" * 78)
    out.append(f"{number}. {title}")
    out.append("=" * 78)


def header(out: list, mode: str, started: datetime, partial: str | None) -> None:
    if partial:
        out.append(f"*** PARTIAL REPORT: {partial} ***")
        out.append("")
    out.append("1. HEADER")
    out.append(f"script:  {Path(__file__).name}  (mode: {mode})")
    out.append(f"time:    {started.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"python:  {sys.version.split()[0]} ({platform.python_implementation()})")
    out.append(f"os:      {platform.platform()}")


def algo_section(out: list, number: int, detector: Detector, blake3_ok: bool, how: str) -> None:
    section(out, number, "ALGORITHM")
    out.append(f"used: {detector.winner}   ({how})")
    if detector.detected:
        out.append(f"detection: {detector.tried} non-empty files tried; "
                   + ", ".join(f"{c}: {n} matches" for c, n in detector.matches.items()))
        if max(detector.matches.values()) == 0:
            out.append("WARNING: no candidate matched any index hash during detection; "
                       "the index may use another algorithm or a partial hash.")
    if not blake3_ok:
        out.append("NOTE: " + BLAKE3_HINT + " (only sha256 was tried)")


def errors_section(out: list, number: int, errors: list) -> None:
    section(out, number, "READ ERRORS (hashing and walking)")
    out.append(f"errors: {len(errors)}")
    for path, message in errors:
        out.append(f"  {path}: {message}")


def write_text(path: Path, lines: list) -> None:
    with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def safe_name(root: str) -> str:
    name = os.path.basename(root.rstrip("\\/")) or os.path.splitdrive(root)[0].rstrip(":\\/") or "root"
    return "".join(c if c not in '<>:"/\\|?*' else "_" for c in name)


def tsv_field(text: str) -> str:
    return text.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


# ------------------------------------------------------------------ mode 1

def backup_report(root, idx, detector, how, files, results, errors, walk_errors,
                  progress, started, elapsed, partial, blake3_ok, tsv_path) -> list:
    out: list[str] = []
    header(out, "backup", started, partial)
    out.append(f"backup:  {root}")
    out.append(f"index:   {idx.path}  ({idx.rows} rows, {len(idx.by_hash)} distinct hashes)")
    out.append("index hash lengths: " + ", ".join(
        f"{length or 'empty'} chars x {n}" for length, n in sorted(idx.lengths.items())))
    out.append(f"tsv:     {tsv_path}")
    algo_section(out, 2, detector, blake3_ok, how)

    index_hashes = set(idx.by_hash)
    matched = [r for r in results if r["hash"] in index_hashes]
    unmatched = [r for r in results if r["hash"] not in index_hashes]
    zero = [r for r in results if r["size"] == 0]
    groups: dict[str, list] = {}
    for r in results:
        if r["size"] > 0:
            groups.setdefault(r["hash"], []).append(r)
    dups = {h: rs for h, rs in groups.items() if len(rs) > 1}
    seen = {r["hash"] for r in results}
    orphans = [(h, p, s) for h, rows in idx.by_hash.items() if h not in seen for p, s in rows]

    section(out, 3, "TOTALS")
    out.append(f"files found: {len(files)}   hashed: {len(results)}   "
               f"({human_size(sum(r['size'] for r in results))})")
    out.append(f"matched an index hash: {len(matched)}")
    out.append(f"NOT in the index: {len(unmatched)}")
    out.append(f"index rows whose hash matches no file in the backup: "
               + (str(len(orphans)) if not partial else f"{len(orphans)} (not meaningful, run is partial)"))
    out.append(f"zero-byte files: {len(zero)}")
    out.append(f"groups of identical files (non-empty): {len(dups)}")
    out.append(f"read errors: {len(errors) + len(walk_errors)}")

    section(out, 4, "FILES NOT IN THE INDEX")
    out.append(f"count: {len(unmatched)}   size: {human_size(sum(r['size'] for r in unmatched))}")
    by_bucket: dict = {label: [0, 0] for label, _ in BUCKETS}
    by_ext: dict = {}
    for r in unmatched:
        b = by_bucket[bucket_of(r["size"])]
        b[0] += 1
        b[1] += r["size"]
        e = by_ext.setdefault(ext_of(r["rel"]), [0, 0])
        e[0] += 1
        e[1] += r["size"]
    out.append("")
    out.append("by size bucket:")
    for label, (n, size) in by_bucket.items():
        out.append(f"  {label:<12} {n:>8} files  {human_size(size):>10}")
    out.append("")
    out.append("by extension:")
    for ext, (n, size) in sorted(by_ext.items(), key=lambda kv: (-kv[1][0], kv[0])):
        out.append(f"  {ext:<12} {n:>8} files  {human_size(size):>10}")
    out.append("")
    out.append("all files (size in bytes, mtime, same-size index rows, relative path):")
    for r in sorted(unmatched, key=lambda r: r["rel"].lower()):
        out.append(f"  {r['size']:>14}  {fmt_time(r['mtime'])}  "
                   f"idx_same_size={idx.sizes.get(r['size'], 0):<4} {plain(r['rel'])}")

    section(out, 5, "INDEX ROWS WITH NO MATCHING FILE IN THE BACKUP")
    out.append(f"rows: {len(orphans)}" + ("   (partial run: files not hashed yet are counted here)" if partial else ""))
    for _h, path, size in sorted(orphans, key=lambda o: str(o[1]).lower()):
        out.append(f"  {size if size is not None else '?':>14}  {path}")

    section(out, 6, "ZERO-BYTE FILES")
    out.append(f"count: {len(zero)}")
    for r in zero:
        out.append(f"  {plain(r['rel'])}")

    section(out, 7, "IDENTICAL FILES INSIDE THE BACKUP")
    out.append(f"groups: {len(dups)}   (zero-byte files excluded; first {DUP_GROUP_CAP} groups shown)")
    for n, (h, rs) in enumerate(sorted(dups.items(), key=lambda kv: -len(kv[1])), 1):
        if n > DUP_GROUP_CAP:
            out.append(f"  ... {len(dups) - DUP_GROUP_CAP} more groups omitted")
            break
        out.append(f"  group {n}: {len(rs)} files, {human_size(rs[0]['size'])} each, hash {h}")
        for r in rs:
            out.append(f"    {plain(r['rel'])}")

    errors_section(out, 8, walk_errors + errors)

    section(out, 9, "TIMING")
    mb = progress.bytes / 1024 ** 2
    out.append(f"elapsed: {human_duration(elapsed)} ({elapsed:.1f}s)   hashed {human_size(progress.bytes)}   "
               f"throughput: {mb / max(elapsed, 1e-9):.1f} MB/s")
    return out


def run_backup(args, root: str, out_dir: Path, blake3_ok: bool) -> int:
    started = datetime.now()
    t0 = time.monotonic()
    stem = f"verify-backup-{safe_name(root)}-{started.strftime('%Y%m%d-%H%M%S')}"
    tsv_path = out_dir / f"{stem}.tsv"
    candidates = [a for a in ALGOS if a != "blake3" or blake3_ok]
    forced = None if args.algo == "auto" else args.algo
    out_dir.mkdir(parents=True, exist_ok=True)

    if forced == "blake3" and not blake3_ok:
        lines: list[str] = []
        header(lines, "backup", started, None)
        lines.append(f"backup:  {root}")
        lines.append("")
        lines.append("ERROR: --algo blake3 was asked for. " + BLAKE3_HINT)
        write_text(out_dir / f"{stem}.log", lines)
        say("ERROR: " + BLAKE3_HINT)
        say(str(out_dir / f"{stem}.log"))
        return 2
    if not blake3_ok and forced is None:
        say("NOTE: " + BLAKE3_HINT + " (only sha256 will be tried)")

    try:
        idx = load_index(root)
    except (RuntimeError, OSError) as exc:
        say(f"cannot read the index: {exc}")
        return 2
    say(f"index: {idx.rows} rows. Counting files...")
    walk_errors: list = []
    files = scan_files(root, True, walk_errors)
    total = sum(e.size for e in files)
    say(f"{len(files)} files, {human_size(total)}. Hashing...")

    detector = Detector(candidates, set(idx.by_hash), forced)
    progress = Progress(len(files), total)
    results: list[dict] = []
    errors: list = []

    def sink(entry: Entry, digest: str) -> None:
        results.append({"rel": entry.rel, "size": entry.size, "mtime": entry.mtime, "hash": digest})

    interrupted = run_hashing(files, detector, progress, sink, errors)
    how = "forced by --algo" if forced else (
        "auto-detected" if detector.detected and len(candidates) > 1 else "only candidate available")
    partial = (f"interrupted by Ctrl+C after {progress.files} of {len(files)} files"
               if interrupted else None)
    elapsed = time.monotonic() - t0
    lines = backup_report(root, idx, detector, how, files, results, errors, walk_errors,
                          progress, started, elapsed, partial, blake3_ok, tsv_path)
    suffix = "-PARTIAL" if interrupted else ""
    log_path = out_dir / f"{stem}{suffix}.log"
    write_text(log_path, lines)

    index_hashes = set(idx.by_hash)
    tsv = [f"#algo={detector.winner}", "relpath\tsize\tmtime\thash\tmatched"]
    for r in results:
        tsv.append("\t".join((tsv_field(plain(r["rel"])), str(r["size"]), f"{r['mtime']:.6f}",
                              r["hash"], "yes" if r["hash"] in index_hashes else "no")))
    write_text(tsv_path, tsv)

    say(str(log_path))
    say(str(tsv_path))
    nmatch = sum(1 for r in results if r["hash"] in index_hashes)
    say(f"{len(results)} hashed, {nmatch} matched, {len(results) - nmatch} not in index, "
        f"{len(errors) + len(walk_errors)} errors")
    return 130 if interrupted else 0


# ------------------------------------------------------------------ mode 2

def run_source(args, root: str, sources: list[str], out_dir: Path, blake3_ok: bool) -> int:
    started = datetime.now()
    t0 = time.monotonic()
    stem = f"verify-source-{safe_name(root)}-{started.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = [a for a in ALGOS if a != "blake3" or blake3_ok]
    forced = None if args.algo == "auto" else args.algo

    idx = None
    try:
        idx = load_index(root)
    except (RuntimeError, OSError) as exc:
        if not args.hashes:
            say(f"cannot read the index: {exc}")
            return 2
        say(f"WARNING: no usable index ({exc}); using only the TSV")
    index_hashes = set(idx.by_hash) if idx else set()
    tsv_hashes: set = set()
    if args.hashes:
        try:
            tsv_algo, tsv_hashes = load_tsv(args.hashes)
        except OSError as exc:
            say(f"cannot read {args.hashes}: {exc}")
            return 2
        if tsv_algo:
            if forced and forced != tsv_algo:
                say(f"--algo {forced} contradicts the TSV header (#algo={tsv_algo})")
                return 2
            forced = tsv_algo
    if forced == "blake3" and not blake3_ok:
        log = out_dir / f"{stem}.log"
        lines: list[str] = []
        header(lines, "source", started, None)
        lines.append("")
        lines.append("ERROR: blake3 is needed (--algo or TSV header). " + BLAKE3_HINT)
        write_text(log, lines)
        say("ERROR: " + BLAKE3_HINT)
        say(str(log))
        return 2
    if forced and forced not in ALGOS:
        say(f"unknown algorithm: {forced}")
        return 2
    if not blake3_ok and forced is None:
        say("NOTE: " + BLAKE3_HINT + " (only sha256 will be tried)")

    walk_errors: list = []
    per_source = []
    for src in sources:
        say(f"counting {src} ...")
        per_source.append((src, scan_files(src, False, walk_errors)))
    all_entries = [e for _s, es in per_source for e in es]
    total = sum(e.size for e in all_entries)
    say(f"{len(all_entries)} files, {human_size(total)} in {len(sources)} source folder(s). Hashing...")

    detector = Detector(candidates, index_hashes | tsv_hashes, forced)
    progress = Progress(len(all_entries), total)
    errors: list = []
    owner: dict[int, str] = {id(e): s for s, es in per_source for e in es}
    results: dict[str, list] = {s: [] for s in sources}
    present_set = index_hashes | tsv_hashes

    # The old index also lists files the old tool converted (HEIC -> JPG), so a
    # hash known only to the index is not proof that the file is in the backup.
    # With a TSV of the backup's real files, only the TSV counts as present.
    def sink(entry: Entry, digest: str) -> None:
        via = ("tsv" if digest in tsv_hashes else "index" if digest in index_hashes else None)
        present = via == "tsv" or (via == "index" and not args.hashes)
        results[owner[id(entry)]].append(
            {"rel": entry.rel, "size": entry.size, "via": via, "present": present})

    interrupted = run_hashing(all_entries, detector, progress, sink, errors)
    how = "forced by --algo / TSV header" if forced else (
        "auto-detected" if detector.detected and len(candidates) > 1 else "only candidate available")
    partial = (f"interrupted by Ctrl+C after {progress.files} of {len(all_entries)} files"
               if interrupted else None)
    elapsed = time.monotonic() - t0

    out: list[str] = []
    header(out, "source", started, partial)
    out.append(f"backup:  {root}")
    out.append(f"index:   {idx.path + f'  ({idx.rows} rows, {len(index_hashes)} distinct hashes)' if idx else '(not used)'}")
    out.append(f"tsv:     {args.hashes or '(none)'}" + (f"  ({len(tsv_hashes)} distinct hashes)" if args.hashes else ""))
    out.append("sources: " + "; ".join(sources))
    algo_section(out, 2, detector, blake3_ok, how)
    for n, (src, _es) in enumerate(per_source, 3):
        rs = results[src]
        present = [r for r in rs if r["present"]]
        missing = [r for r in rs if not r["present"]]
        index_only = [r for r in missing if r["via"] == "index"]
        section(out, n, f"SOURCE {src}")
        out.append(f"files hashed: {len(rs)} of {len(_es)}   size: {human_size(sum(r['size'] for r in rs))}")
        if args.hashes:
            out.append(f"present in the backup (a file with the same content is in it): {len(present)}")
        else:
            out.append(f"present according to the old index (no TSV given, files not checked): {len(present)}")
        out.append(f"MISSING from the backup: {len(missing)}   ({human_size(sum(r['size'] for r in missing))})")
        if args.hashes:
            out.append(f"  of which known to the old index but not in the backup as such "
                       f"(e.g. HEIC converted to JPG): {len(index_only)}   "
                       f"({human_size(sum(r['size'] for r in index_only))})")
        exts: dict = {}
        for r in rs:
            e = exts.setdefault(ext_of(r["rel"]), [0, 0])
            e[0 if r["present"] else 1] += 1
        out.append("")
        out.append("per extension (kind per ibackup.mediameta; * = skipped by the old tool, expected missing):")
        out.append(f"  {'ext':<12} {'present':>8} {'missing':>8}  kind")
        for ext, (p, m) in sorted(exts.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0])):
            star = " *" if ext in OLD_TOOL_SKIPPED else ""
            out.append(f"  {ext:<12} {p:>8} {m:>8}  {classify(ext)}{star}")
        out.append("")
        out.append("missing files by extension (size in bytes, relative path):")
        by_ext: dict = {}
        for r in missing:
            by_ext.setdefault(ext_of(r["rel"]), []).append(r)
        for ext, rows in sorted(by_ext.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            out.append(f"  [{ext}] {len(rows)} files, {human_size(sum(r['size'] for r in rows))}")
            for r in sorted(rows, key=lambda r: r["rel"].lower()):
                mark = "  [old index only]" if r["via"] == "index" else ""
                out.append(f"    {r['size']:>14}  {plain(r['rel'])}{mark}")
    n = 3 + len(per_source)
    errors_section(out, n, walk_errors + errors)
    section(out, n + 1, "TIMING")
    out.append(f"elapsed: {human_duration(elapsed)} ({elapsed:.1f}s)   hashed {human_size(progress.bytes)}   "
               f"throughput: {progress.bytes / 1024 ** 2 / max(elapsed, 1e-9):.1f} MB/s")

    log_path = out_dir / f"{stem}{'-PARTIAL' if interrupted else ''}.log"
    write_text(log_path, out)
    say(str(log_path))
    miss = sum(1 for rs in results.values() for r in rs if not r["present"])
    say(f"{sum(len(rs) for rs in results.values())} hashed, {miss} missing from the backup, "
        f"{len(errors) + len(walk_errors)} errors")
    return 130 if interrupted else 0


# -------------------------------------------------------------------- main

def inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def main(argv=None, out_dir=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only hash verification of a photo backup.")
    sub = parser.add_subparsers(dest="mode", required=True)
    p1 = sub.add_parser("backup", help="hash the backup and compare with its index")
    p1.add_argument("backup")
    p2 = sub.add_parser("source", help="check that source folders' content exists in the backup")
    p2.add_argument("backup")
    p2.add_argument("sources", nargs="+")
    p2.add_argument("--hashes", help="TSV written by mode 1")
    for p in (p1, p2):
        p.add_argument("--algo", choices=("auto",) + ALGOS, default="auto")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.backup)
    sources = [os.path.abspath(s) for s in getattr(args, "sources", [])]
    for folder in [root] + sources:
        if not os.path.isdir(fs_path(folder)):
            print(f"not a folder: {folder}", file=sys.stderr)
            return 2
    out_path = (Path(out_dir) if out_dir else Path(__file__).resolve().parent).resolve()
    for folder in [root] + sources:
        if inside(out_path, Path(folder).resolve()):
            print(f"refusing to write reports inside the inspected folder: {out_path}", file=sys.stderr)
            return 2

    blake3_ok = blake3_available()
    if not blake3_ok and args.algo in ("auto", "blake3"):
        blake3_ok = offer_blake3_install()
    if args.mode == "backup":
        return run_backup(args, root, out_path, blake3_ok)
    return run_source(args, root, sources, out_path, blake3_ok)


if __name__ == "__main__":
    raise SystemExit(main())
