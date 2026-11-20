"""tools/rescue_missing.py: copy the files a source-mode report lists as missing."""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import sys
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rescue = load("rescue_missing")
verify_backup = load("verify_backup")

OLD_MTIME = 1_500_000_000


def snapshot(root: Path) -> dict:
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, p.read_bytes())
    return snap


def write(path: Path, data: bytes, mtime: int = OLD_MTIME) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def ext_of(rel: str) -> str:
    return os.path.splitext(rel.lower())[1] or "(none)"


def report_text(source, backup, rows, *, partial=False, not_checked=0,
                missing=None, mode="source") -> str:
    """A report in the layout verify_backup.py source mode writes.

    rows: (relative path, size, known to the old index only)."""
    out = []
    if partial:
        out += ["*** PARTIAL REPORT: interrupted by Ctrl+C after 1 of 2 files ***", ""]
    out += ["1. HEADER", f"script:  verify_backup.py  (mode: {mode})",
            "time:    2026-11-20 21:00:47", f"backup:  {backup}",
            f"sources: {source}; D:\\other source", "",
            "=" * 78, "2. ALGORITHM", "=" * 78, "used: sha256   (forced by --algo / TSV header)", "",
            "=" * 78, f"3. SOURCE {source}", "=" * 78,
            f"files hashed: {len(rows) + 1} of {len(rows) + 1}   size: 1 KB"]
    if not_checked:
        out.append("NOT CHECKED (unreadable files or folders, or interrupted; treat as NOT "
                   f"backed up, see the errors section): {not_checked}")
    out.append("present in the backup (a file with the same content is in it): 1   (1 KB, x)")
    out.append(f"MISSING from the backup: {len(rows) if missing is None else missing}   (1 KB)")
    out += ["", "missing files by extension (size in bytes, relative path):"]
    by_ext = {}
    for row in rows:
        by_ext.setdefault(ext_of(row[0]), []).append(row)
    for ext, group in by_ext.items():
        out.append(f"  [{ext}] {len(group)} files, 1 KB")
        for rel, size, index_only in group:
            out.append(f"    {size:>14}  {rel}" + ("  [old index only]" if index_only else ""))
    out += ["", "=" * 78, "4. SOURCE D:\\other source", "=" * 78,
            "files hashed: 1 of 1   size: 1 KB",
            "MISSING from the backup: 1   (1 KB)", "",
            "missing files by extension (size in bytes, relative path):",
            "  [.jpg] 1 files, 10 B", "                10  elsewhere.jpg", "",
            "=" * 78, "5. READ ERRORS (hashing and walking)", "=" * 78, "errors: 0"]
    return "\n".join(out) + "\n"


Run = namedtuple("Run", "code out err")


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.backup = self.base / "backup_final"
        self.source = self.base / "!backup"
        self.dest = self.base / "salvare backup"
        self.out_dir = self.base / "logs"
        write(self.backup / "2019" / "a.jpg", b"jpg in both")
        self.files = {
            "IMG_0008.HEIC": b"heic original" * 100,
            os.path.join("sub dir!", "IMG 0009!.HEIC"): b"second heic",
            "IMG_0008.AAE": b"<plist/>",
            os.path.join("sub dir!", "deep", "x.webp"): b"webp bytes",
        }
        for rel, data in self.files.items():
            write(self.source / rel, data)
        write(self.source / "a.jpg", b"jpg in both")
        self.rows = [(rel, len(data), rel.endswith(".HEIC")) for rel, data in self.files.items()]
        self.report = self.base / "verify-source.log"
        self.write_report(self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def write_report(self, rows, **kwargs):
        self.report.write_text(report_text(self.source, self.backup, rows, **kwargs), encoding="utf-8")

    def run_main(self, *extra, report=None, source=None, dest=None) -> Run:
        argv = [str(report or self.report), str(source or self.source), str(dest or self.dest), *extra]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rescue.main(argv, out_dir=self.out_dir)
        return Run(code, out.getvalue(), err.getvalue())

    def log(self) -> str:
        logs = sorted(self.out_dir.glob("*.log"))
        self.assertEqual(len(logs), 1, logs)
        return logs[0].read_text(encoding="utf-8")

    def assert_sources_untouched(self, before_source, before_backup):
        self.assertEqual(snapshot(self.source), before_source)
        self.assertEqual(snapshot(self.backup), before_backup)

    def test_copies_every_listed_file_verified_with_its_mtime(self):
        before = snapshot(self.source), snapshot(self.backup)
        run = self.run_main()
        self.assertEqual(run.code, 0, run)
        self.assert_sources_untouched(*before)
        self.assertEqual(sorted(snapshot(self.dest)), sorted(self.files))
        for rel, data in self.files.items():
            target = self.dest / rel
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(target.stat().st_mtime_ns, (self.source / rel).stat().st_mtime_ns)
        self.assertFalse((self.dest / "a.jpg").exists())
        log = self.log()
        for rel in self.files:
            self.assertIn(rel, log)
        self.assertIn("copied and verified: 4", log)
        self.assertIn("already there (identical): 0", log)
        self.assertIn("failed: 0", log)
        self.assertIn("copied and verified: 4", run.out)

    def test_relative_paths_work(self):
        cwd = os.getcwd()
        os.chdir(self.base)
        try:
            run = self.run_main(report=Path(self.report.name), source=Path("!backup"),
                                dest=Path("salvare backup"))
        finally:
            os.chdir(cwd)
        self.assertEqual(run.code, 0, run)
        self.assertEqual(sorted(snapshot(self.dest)), sorted(self.files))

    def test_dry_run_lists_and_writes_nothing_in_the_destination(self):
        before = snapshot(self.source), snapshot(self.backup)
        run = self.run_main("--dry-run")
        self.assertEqual(run.code, 0, run)
        self.assertFalse(self.dest.exists())
        self.assert_sources_untouched(*before)
        log = self.log()
        for rel in self.files:
            self.assertIn(rel, log)
        total = sum(len(d) for d in self.files.values())
        self.assertIn(f"would copy: 4 files, {total} bytes", log)
        self.assertIn(f"would copy: 4 files, {total} bytes", run.out)

    def test_an_identical_target_counts_as_already_there(self):
        rel = "IMG_0008.HEIC"
        write(self.dest / rel, self.files[rel], mtime=OLD_MTIME + 99)
        run = self.run_main()
        self.assertEqual(run.code, 0, run)
        self.assertEqual((self.dest / rel).stat().st_mtime, OLD_MTIME + 99)
        self.assertIn("copied and verified: 3", self.log())
        self.assertIn("already there (identical): 1", self.log())

    def test_a_different_target_is_never_overwritten(self):
        rel = os.path.join("sub dir!", "IMG 0009!.HEIC")
        # One of another size, one of the source's size (b"second heic").
        for other in (b"something else", b"second heix"):
            with self.subTest(other=other):
                write(self.dest / rel, other)
                run = self.run_main()
                self.assertEqual(run.code, 1, run)
                self.assertEqual((self.dest / rel).read_bytes(), other)
                log = self.log()
                self.assertIn("failed: 1", log)
                self.assertIn("exists with different content", log)
                for f in self.out_dir.glob("*.log"):
                    f.unlink()

    def test_a_source_that_shrinks_during_the_copy_is_not_kept(self):
        real_open = open

        def short_open(path, mode="r", *args, **kwargs):
            fh = real_open(path, mode, *args, **kwargs)
            if str(path).endswith("IMG_0008.HEIC") and "r" in mode:
                data = fh.read()[:-5]
                fh.close()
                return io.BytesIO(data)
            return fh

        with mock.patch.object(rescue, "open", short_open, create=True):
            run = self.run_main()
        self.assertEqual(run.code, 1, run)
        self.assertFalse((self.dest / "IMG_0008.HEIC").exists())
        self.assertEqual(list(self.dest.rglob("*.partial")), [])
        self.assertIn("the source changed during the copy", self.log())

    def test_a_read_error_fails_that_file_only_and_leaves_no_partial(self):
        real_open = open

        class Broken:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

            def read(self, n=-1):
                raise OSError(5, "Access is denied (simulated)")

        def flaky_open(path, mode="r", *args, **kwargs):
            fh = real_open(path, mode, *args, **kwargs)
            if str(path).endswith("IMG_0008.AAE") and "r" in mode:
                return Broken(fh)
            return fh

        before = snapshot(self.source), snapshot(self.backup)
        with mock.patch.object(rescue, "open", flaky_open, create=True):
            run = self.run_main()
        self.assertEqual(run.code, 1, run)
        self.assert_sources_untouched(*before)
        self.assertEqual(sorted(snapshot(self.dest)),
                         sorted(r for r in self.files if r != "IMG_0008.AAE"))
        log = self.log()
        self.assertIn("failed: 1", log)
        self.assertIn("simulated", log)

    def test_a_copy_that_does_not_verify_is_not_kept(self):
        real = rescue.hash_file

        def corrupt(path, algo):
            digest = real(path, algo)
            return "0" * len(digest) if path.endswith(".partial") and "x.webp" in path else digest

        with mock.patch.object(rescue, "hash_file", corrupt):
            run = self.run_main()
        self.assertEqual(run.code, 1, run)
        self.assertEqual(sorted(snapshot(self.dest)),
                         sorted(r for r in self.files if not r.endswith("x.webp")))
        self.assertIn("copy does not match the source", self.log())

    def test_a_source_file_whose_size_changed_is_not_copied(self):
        rows = [(r, s + 1 if r == "IMG_0008.AAE" else s, i) for r, s, i in self.rows]
        self.write_report(rows)
        run = self.run_main()
        self.assertEqual(run.code, 1, run)
        self.assertFalse((self.dest / "IMG_0008.AAE").exists())
        self.assertIn("size differs from the report", self.log())

    def test_a_listed_file_gone_from_the_source_fails(self):
        self.write_report(self.rows + [("gone.heic", 5, False)])
        run = self.run_main()
        self.assertEqual(run.code, 1, run)
        self.assertIn("failed: 1", self.log())

    def test_interrupt_removes_the_partial_copy_and_marks_the_log(self):
        real = rescue.hash_file
        seen = []

        def interrupted(path, algo):
            if path.endswith(".partial"):
                seen.append(os.path.exists(path))
                if len(seen) == 2:
                    raise KeyboardInterrupt
            return real(path, algo)

        with mock.patch.object(rescue, "hash_file", interrupted):
            run = self.run_main()
        self.assertEqual(run.code, 130, run)
        self.assertEqual(seen, [True, True])
        self.assertEqual(list(self.dest.rglob("*.partial")), [])
        self.assertEqual(len(snapshot(self.dest)), 1)
        self.assertIn("PARTIAL", self.log())
        self.assertIn("not reached (interrupted): 3", self.log())

    def test_an_identical_target_with_another_mtime_is_noted(self):
        rel = "IMG_0008.HEIC"
        write(self.dest / rel, self.files[rel], mtime=OLD_MTIME + 99)
        self.assertEqual(self.run_main().code, 0)
        self.assertIn("modification time differs", self.log())

    def test_a_name_with_a_line_separator_character_is_copied(self):
        rel = "odd\x85name .heic"
        write(self.source / rel, b"odd")
        self.write_report(self.rows + [(rel, 3, False)])
        run = self.run_main()
        self.assertEqual(run.code, 0, run)
        self.assertEqual((self.dest / rel).read_bytes(), b"odd")

    def test_dry_run_without_enough_space_says_so(self):
        usage = namedtuple("usage", "total used free")(100, 99, 1)
        with mock.patch.object(rescue.shutil, "disk_usage", return_value=usage):
            run = self.run_main("--dry-run")
        self.assertEqual(run.code, 1, run)
        self.assertIn("NOT ENOUGH FREE SPACE", run.out)

    def test_not_enough_free_space_is_refused(self):
        usage = namedtuple("usage", "total used free")(100, 99, 1)
        with mock.patch.object(rescue.shutil, "disk_usage", return_value=usage):
            run = self.run_main()
        self.assertEqual(run.code, 2, run)
        self.assertFalse(self.dest.exists())
        self.assertIn("not enough free space", run.err)

    def refused(self, run: Run, text: str):
        self.assertEqual(run.code, 2, run)
        self.assertIn(text, run.err)

    def test_a_destination_inside_the_source_is_refused(self):
        self.refused(self.run_main(dest=self.source / "copy"), "overlaps")
        self.assertFalse((self.source / "copy").exists())

    def test_a_destination_inside_the_backup_is_refused(self):
        self.refused(self.run_main(dest=self.backup / "copy"), "overlaps")
        self.assertFalse((self.backup / "copy").exists())

    @unittest.skipUnless(os.name == "nt", "\\\\?\\ paths are Windows only")
    def test_an_extended_form_destination_inside_the_source_is_refused(self):
        self.refused(self.run_main(dest="\\\\?\\" + str(self.source / "copy")), "overlaps")
        self.assertFalse((self.source / "copy").exists())

    def test_a_destination_inside_a_backup_the_report_names_elsewhere_is_refused(self):
        moved = self.base / "backup on another letter"
        (moved / ".ibackup").mkdir(parents=True)
        self.refused(self.run_main(dest=moved / "copy"), "inside a backup")
        self.assertFalse((moved / "copy").exists())

    def test_a_destination_that_contains_the_source_is_refused(self):
        self.refused(self.run_main(dest=self.base), "overlaps")

    def test_logs_inside_the_source_are_refused(self):
        self.out_dir = self.source / "logs"
        self.refused(self.run_main(), "inside")
        self.assertFalse(self.out_dir.exists())

    def test_a_partial_report_is_refused(self):
        self.write_report(self.rows, partial=True)
        self.refused(self.run_main(), "interrupted")

    def test_a_report_with_unchecked_files_is_refused(self):
        self.write_report(self.rows, not_checked=2)
        self.refused(self.run_main(), "not checked")

    def test_a_count_that_does_not_add_up_is_refused(self):
        self.write_report(self.rows, missing=5)
        self.refused(self.run_main(), "says 5 missing")

    def test_a_backup_mode_report_is_refused(self):
        self.write_report(self.rows, mode="backup")
        self.refused(self.run_main(), "source-mode report")

    def test_a_source_not_in_the_report_is_refused(self):
        other = self.base / "elsewhere"
        other.mkdir()
        self.refused(self.run_main(source=other), "no section")

    def test_a_path_leaving_the_destination_is_refused(self):
        self.write_report(self.rows + [(os.path.join("..", "evil.heic"), 3, False)])
        self.refused(self.run_main(), "leaves")
        self.assertFalse((self.base / "evil.heic").exists())

    def test_a_rooted_path_is_refused(self):
        self.write_report(self.rows + [(os.sep + "evil.heic", 3, False)])
        self.refused(self.run_main(), "leaves")


class EndToEndTests(unittest.TestCase):
    """The report comes from verify_backup.py itself, so a format change shows up here."""

    def test_rescues_exactly_what_verify_backup_found_missing(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            backup, source, dest, logs = (base / "backup_final", base / "!backup",
                                          base / "salvare", base / "logs")
            write(backup / "2019" / "a.jpg", b"jpg in both")
            (backup / ".ibackup").mkdir()
            con = sqlite3.connect(backup / ".ibackup" / "index.db")
            con.execute("CREATE TABLE media_index(path TEXT, size_bytes INTEGER, mtime REAL, hash TEXT)")
            con.commit()
            con.close()
            write(source / "a.jpg", b"jpg in both")
            write(source / "IMG 1!.HEIC", b"only here")
            write(source / "d" / "x.aae", b"sidecar")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(verify_backup.main(["backup", str(backup), "--algo", "sha256"],
                                                    out_dir=logs), 0)
                tsv = next(logs.glob("*.tsv"))
                self.assertEqual(verify_backup.main(["source", str(backup), str(source),
                                                     "--hashes", str(tsv), "--algo", "sha256"], out_dir=logs), 0)
            report = next(logs.glob("verify-source-*.log"))
            before = snapshot(source)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                code = rescue.main([str(report), str(source), str(dest)], out_dir=base / "rlogs")
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(snapshot(source), before)
            self.assertEqual(sorted(snapshot(dest)), sorted(["IMG 1!.HEIC", os.path.join("d", "x.aae")]))


if __name__ == "__main__":
    sys.exit(unittest.main())
