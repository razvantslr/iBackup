"""Copy the files a verify_backup.py source report lists as missing from the backup.

Reads the "missing files by extension" list of one SOURCE section and copies
each file to <dest>\\<relative path>. Nothing is written inside the source or
the backup: the destination may not overlap either, an existing target is
never overwritten (identical content counts as already there), and every copy
is written as <name>.partial, hashed back and only then renamed. Modification
times are kept. A second run over the same destination re-hashes every target
against its source, so it doubles as a later verification.

    python tools/rescue_missing.py <verify-source report> D:\\poze\\!backup C:\\salvare-backup --dry-run
    python tools/rescue_missing.py <verify-source report> D:\\poze\\!backup C:\\salvare-backup

Writes rescue-<source name>-<YYYYMMDD-HHMMSS>.log next to this script (with
-dryrun before the name for a dry run) and prints its path. Exit code: 0 all
done, 1 some files failed, 2 refused, 130 Ctrl+C. blake3 is used if installed,
else sha256.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_backup import fs_path, plain                             # noqa: E402
from verify_backup import (CHUNK, META_DIR, Progress, blake3_available,  # noqa: E402
                           inside, make_hasher, safe_name, same_path, say, section,
                           write_text)
from ibackup.util import human_duration, human_size                  # noqa: E402

INDEX_ONLY_MARK = "  [old index only]"
FREE_MARGIN = 256 * 1024 ** 2
PARTIAL_SUFFIX = ".partial"


class ReportError(Exception):
    pass


# ------------------------------------------------------------------ report

def parse_report(path: str, source: str) -> tuple[str, list]:
    """The backup folder named in the report and the (relative path, size)
    rows listed as missing for `source`. Raises ReportError when the list
    cannot be trusted to be complete."""
    with open(fs_path(path), "r", encoding="utf-8-sig", errors="replace") as fh:
        # Not splitlines(): it also splits on NEL and LINE SEPARATOR, legal in file names.
        lines = [line.rstrip("\r") for line in fh.read().split("\n")]
    if lines and lines[0].startswith("*** PARTIAL REPORT"):
        raise ReportError("the report comes from an interrupted run; run verify_backup.py to the end first")
    if not any(re.match(r"^script:\s+verify_backup\.py\s+\(mode: source\)", line) for line in lines[:10]):
        raise ReportError(f"{path} is not a verify_backup.py source-mode report")
    backup = next((line.split(":", 1)[1].strip() for line in lines[:10] if line.startswith("backup:")), "")
    if not backup:
        raise ReportError("the report does not name its backup folder")

    sections, start = [], None
    for i, line in enumerate(lines):
        m = re.match(r"^\d+\. SOURCE (.+)$", line)
        if m and i > 0 and lines[i - 1].startswith("====="):
            sections.append(m.group(1))
            if same_path(m.group(1), source):
                start = i + 2
    if start is None:
        raise ReportError(f"the report has no section for {source}; it has: "
                          + ("; ".join(sections) or "(none)"))

    expected, missing, rows, ext, listing = {}, None, [], None, False
    for line in lines[start:]:
        if line.startswith("====="):
            break
        if line.startswith("NOT CHECKED"):
            raise ReportError(f"some files in {source} were not checked ({line.split(':')[-1].strip()}), "
                              f"so the missing list is incomplete; fix the read errors and run "
                              f"verify_backup.py again")
        m = re.match(r"^MISSING from the backup: (\d+)", line)
        if m:
            missing = int(m.group(1))
        if line.startswith("missing files by extension"):
            listing = True
            continue
        if not listing or not line.strip():
            continue
        m = re.match(r"^  \[(.+)\] (\d+) files, ", line)
        if m:
            ext = m.group(1)
            expected[ext] = [int(m.group(2)), 0]
            continue
        m = re.match(r"^ {4,}(\d+)  (.+)$", line)
        if not m or ext is None:
            raise ReportError(f"cannot read this line of the missing list: {line!r}")
        rel = m.group(2)
        if rel.endswith(INDEX_ONLY_MARK):
            rel = rel[:-len(INDEX_ONLY_MARK)]
        check_relative(rel)
        rows.append((rel, int(m.group(1))))
        expected[ext][1] += 1

    if missing is None:
        raise ReportError(f"the section for {source} has no 'MISSING from the backup' line")
    if len(rows) != missing:
        raise ReportError(f"the report says {missing} missing files for {source} but lists {len(rows)}")
    for ext, (said, listed) in expected.items():
        if said != listed:
            raise ReportError(f"the report says {said} {ext} files but lists {listed}")
    return backup, rows


def check_relative(rel: str) -> None:
    norm = os.path.normpath(rel)
    if (os.path.isabs(norm) or os.path.splitdrive(norm)[0] or norm.startswith(("\\", "/"))
            or norm.split(os.sep)[0] in ("..", ".")):
        raise ReportError(f"the listed path {rel!r} leaves the source folder")


# ------------------------------------------------------------------- copy

def hash_file(path: str, algo: str) -> str:
    h = make_hasher(algo)
    with open(fs_path(path), "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def describe(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc}"


def copy_one(src: str, target: str, size: int, algo: str) -> tuple[str, str]:
    """('copied' | 'already' | 'failed', detail). Never replaces an existing
    target; a .partial this call created is removed on any failure or Ctrl+C."""
    try:
        st = os.stat(fs_path(src))
    except OSError as exc:
        return "failed", f"cannot read the source: {describe(exc)}"
    if not stat.S_ISREG(st.st_mode):
        return "failed", "the source is not a file"
    if st.st_size != size:
        return "failed", f"size differs from the report ({st.st_size} now, {size} in the report)"
    if os.path.lexists(fs_path(target)):
        try:
            if os.path.samefile(fs_path(src), fs_path(target)):
                return "failed", "the target is the source file itself (a link in the destination?)"
            tst = os.stat(fs_path(target))
            same = tst.st_size == size and hash_file(target, algo) == hash_file(src, algo)
        except OSError as exc:
            return "failed", f"the target exists and cannot be compared: {describe(exc)}"
        if not same:
            return "failed", "the target exists with different content; left as it is"
        if tst.st_mtime_ns != st.st_mtime_ns:
            return "already", "identical content, but its modification time differs from the source"
        return "already", "identical file already there"

    partial = target + PARTIAL_SUFFIX
    if os.path.lexists(fs_path(partial)):
        return "failed", f"{partial} is in the way (left by a crashed run?); delete it and run again"
    created = False
    try:
        os.makedirs(fs_path(os.path.dirname(target)), exist_ok=True)
        h = make_hasher(algo)
        copied = 0
        with open(fs_path(partial), "xb") as out:
            created = True
            with open(fs_path(src), "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    copied += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        if copied != size:
            return "failed", f"read {copied} bytes, the report says {size}; the source changed during the copy"
        os.utime(fs_path(partial), ns=(st.st_atime_ns, st.st_mtime_ns))
        if hash_file(partial, algo) != h.hexdigest():
            return "failed", "the copy does not match the source; it was not kept"
        # On Windows os.rename never replaces an existing file.
        os.rename(fs_path(partial), fs_path(target))
        created = False
        return "copied", "copied and verified"
    except OSError as exc:
        return "failed", describe(exc)
    finally:
        if created:
            try:
                os.remove(fs_path(partial))
            except OSError:
                pass


# ------------------------------------------------------------------- main

def existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def header(out: list, mode: str, started: datetime, args, backup: str, algo: str,
           rows: list, free: int, partial: str | None) -> None:
    if partial:
        out.append(f"*** PARTIAL LOG: {partial} ***")
        out.append("")
    out.append("1. HEADER")
    out.append(f"script:  {Path(__file__).name}  (mode: {mode})")
    out.append(f"time:    {started.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"python:  {sys.version.split()[0]} ({platform.python_implementation()})")
    out.append(f"os:      {platform.platform()}")
    out.append(f"report:  {args.report}")
    out.append(f"source:  {args.source}")
    out.append(f"backup:  {backup}  (from the report; not read)")
    out.append(f"dest:    {args.dest}  (free {human_size(free)})")
    out.append(f"algorithm: {algo}")
    out.append(f"listed as missing: {len(rows)} files, {human_size(sum(s for _r, s in rows))}")


def dry_run(args, backup, rows, algo, free, out_path: Path, started: datetime) -> int:
    plan, problems, exists = [], [], 0
    copy_n = copy_bytes = 0
    for rel, size in rows:
        src = os.path.join(args.source, rel)
        try:
            st = os.stat(fs_path(src))
            note = (None if stat.S_ISREG(st.st_mode) and st.st_size == size
                    else "the source is not a file" if not stat.S_ISREG(st.st_mode)
                    else f"size differs from the report ({st.st_size} now)")
        except OSError as exc:
            note = f"cannot read the source: {describe(exc)}"
        if note:
            problems.append(rel)
            plan.append(f"  PROBLEM    {size:>14}  {rel}  ({note})")
        elif os.path.lexists(fs_path(os.path.join(args.dest, rel))):
            exists += 1
            plan.append(f"  exists     {size:>14}  {rel}  (will be compared, never overwritten)")
        else:
            copy_n += 1
            copy_bytes += size
            plan.append(f"  copy       {size:>14}  {rel}")
    out: list[str] = []
    header(out, "dry run", started, args, backup, algo, rows, free, None)
    section(out, 2, "SUMMARY (dry run: nothing was copied)")
    summary = f"would copy: {copy_n} files, {copy_bytes} bytes ({human_size(copy_bytes)})"
    out.append(summary)
    out.append(f"targets already in the destination (compared during the real run): {exists}")
    out.append(f"problems (would fail): {len(problems)}")
    space = ""
    if free < copy_bytes + FREE_MARGIN:
        space = (f"NOT ENOUGH FREE SPACE in the destination: {human_size(free)} free, "
                 f"{human_size(copy_bytes + FREE_MARGIN)} needed")
        out.append(space)
    section(out, 3, "ALL FILES (action, size in bytes, relative path)")
    out.extend(plan)
    log = out_path / f"rescue-dryrun-{safe_name(args.source)}-{started.strftime('%Y%m%d-%H%M%S')}.log"
    write_text(log, out)
    say(str(log))
    say(f"{summary}; {exists} already in the destination; {len(problems)} problems")
    if space:
        say(space)
    return 1 if problems or space else 0


def run_copy(args, backup, rows, algo, free, out_path: Path, started: datetime) -> int:
    t0 = time.monotonic()
    total = sum(s for _r, s in rows)
    progress = Progress(len(rows), total)
    results: list = []
    interrupted = False
    try:
        for rel, size in rows:
            status, detail = copy_one(os.path.join(args.source, rel), os.path.join(args.dest, rel),
                                      size, algo)
            results.append((status, rel, size, detail))
            progress.files += 1
            progress.add(size)
    except KeyboardInterrupt:
        interrupted = True
    elapsed = time.monotonic() - t0

    counts = {k: sum(1 for r in results if r[0] == k) for k in ("copied", "already", "failed")}
    copied_bytes = sum(r[2] for r in results if r[0] == "copied")
    partial = (f"interrupted by Ctrl+C after {len(results)} of {len(rows)} files"
               if interrupted else None)
    out: list[str] = []
    header(out, "copy", started, args, backup, algo, rows, free, partial)
    section(out, 2, "SUMMARY")
    summary = (f"copied and verified: {counts['copied']}   already there (identical): "
               f"{counts['already']}   failed: {counts['failed']}   of {len(rows)} listed")
    out.append(f"copied and verified: {counts['copied']}   ({human_size(copied_bytes)})")
    out.append(f"already there (identical): {counts['already']}")
    out.append(f"failed: {counts['failed']}")
    out.append(f"not reached (interrupted): {len(rows) - len(results)}")
    out.append(f"verified in the destination (copied + already there): "
               f"{counts['copied'] + counts['already']} of {len(rows)}")
    section(out, 3, "FAILED FILES")
    out.append(f"count: {counts['failed']}")
    for status, rel, size, detail in results:
        if status == "failed":
            out.append(f"  {size:>14}  {rel}  ({detail})")
    section(out, 4, "ALL FILES (result, size in bytes, relative path)")
    labels = {"copied": "copied ", "already": "already", "failed": "FAILED "}
    for status, rel, size, detail in results:
        quiet = detail in ("copied and verified", "identical file already there")
        out.append(f"  {labels[status]}  {size:>14}  {rel}" + ("" if quiet else f"  ({detail})"))
    section(out, 5, "TIMING")
    out.append(f"elapsed: {human_duration(elapsed)} ({elapsed:.1f}s)   "
               f"copied {human_size(copied_bytes)}   "
               f"throughput: {copied_bytes / 1024 ** 2 / max(elapsed, 1e-9):.1f} MB/s")

    suffix = "-PARTIAL" if interrupted else ""
    log = out_path / f"rescue-{safe_name(args.source)}-{started.strftime('%Y%m%d-%H%M%S')}{suffix}.log"
    write_text(log, out)
    say(str(log))
    say(summary)
    if interrupted:
        return 130
    return 1 if counts["failed"] else 0


def refuse(text: str) -> int:
    print(f"refusing: {text}", file=sys.stderr)
    return 2


def main(argv=None, out_dir=None) -> int:
    parser = argparse.ArgumentParser(description="Copy the files a verify_backup.py source "
                                                 "report lists as missing, verified.")
    parser.add_argument("report", help="the verify-source-*.log written by verify_backup.py source")
    parser.add_argument("source", help="the source folder, as named in the report")
    parser.add_argument("dest", help="where the copies go (created if needed)")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be copied")
    args = parser.parse_args(argv)

    # fs_path's \\?\ prefix only works on absolute paths.
    # plain() first, so a \\?\ form of a path cannot slip past the overlap checks.
    args.report, args.source, args.dest = (os.path.abspath(plain(p))
                                           for p in (args.report, args.source, args.dest))
    if not os.path.isfile(fs_path(args.report)):
        return refuse(f"not a file: {args.report}")
    if not os.path.isdir(fs_path(args.source)):
        return refuse(f"not a folder: {args.source}")
    try:
        backup, rows = parse_report(args.report, args.source)
    except ReportError as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot read {args.report}: {describe(exc)}")

    out_path = (Path(out_dir) if out_dir else Path(__file__).resolve().parent).resolve()
    dest_path = Path(args.dest).resolve()
    for folder in (args.source, plain(backup)):
        folder_path = Path(folder).resolve()
        if inside(out_path, folder_path):
            return refuse(f"the log would be written inside {folder}: {out_path}")
        if inside(dest_path, folder_path) or inside(folder_path, dest_path):
            return refuse(f"the destination {args.dest} overlaps {folder}")
    # The report's backup path may be stale (another drive letter): also refuse
    # any destination inside a folder that holds a backup index.
    for folder in [dest_path, *dest_path.parents]:
        if (folder / META_DIR).is_dir():
            return refuse(f"the destination {args.dest} is inside a backup ({folder} has {META_DIR})")

    algo = "blake3" if blake3_available() else "sha256"
    try:
        free = shutil.disk_usage(existing_ancestor(dest_path)).free
    except OSError as exc:
        return refuse(f"cannot read the free space for {args.dest}: {describe(exc)}")
    started = datetime.now()
    out_path.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        return dry_run(args, backup, rows, algo, free, out_path, started)
    need = sum(s for rel, s in rows if not os.path.lexists(fs_path(os.path.join(args.dest, rel))))
    if free < need + FREE_MARGIN:
        return refuse(f"not enough free space in {existing_ancestor(dest_path)}: {human_size(free)} free, "
                      f"{human_size(need + FREE_MARGIN)} needed")
    try:
        os.makedirs(fs_path(args.dest), exist_ok=True)
    except OSError as exc:
        return refuse(f"cannot create {args.dest}: {describe(exc)}")
    return run_copy(args, backup, rows, algo, free, out_path, started)


if __name__ == "__main__":
    raise SystemExit(main())
