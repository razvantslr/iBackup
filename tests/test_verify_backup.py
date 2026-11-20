"""tools/verify_backup.py: hash comparison with a foreign index, and read-only behaviour."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "verify_backup.py"
spec = importlib.util.spec_from_file_location("verify_backup", SCRIPT)
verify_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_backup)

HAVE_BLAKE3 = verify_backup.blake3_available()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b3(data: bytes) -> str:
    import blake3
    return blake3.blake3(data).hexdigest()


def snapshot(root: Path) -> dict:
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, p.read_bytes())
    return snap


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class VerifyBackupTests(unittest.TestCase):
    hash_fn = staticmethod(sha)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(self.tmp.name)
        self.backup = base / "backup \u03b5\u03bb"
        self.source = base / "source"
        self.out_dir = base / "out"
        h = self.hash_fn
        b = self.backup
        self.good = b"good photo content" * 50
        self.orig_bad = b"original content of bad"
        self.dup = b"identical bytes"
        write(b / "2019" / "07_July" / "good.jpg", self.good)
        write(b / "2019" / "07_July" / "bad.jpg", b"CORRUPTED content of bad")
        write(b / "2019" / "07_July" / "extra.jpg", b"not in the index")
        write(b / "2019" / "07_July" / "\u03a0\u03b1\u03c4\u03c1\u03b1" / "zero.jpg", b"")
        write(b / "2020" / "01_January" / "dup1.jpg", self.dup)
        write(b / "2020" / "01_January" / "dup2.jpg", self.dup)
        write(b / ".ibackup" / "ignored.txt", b"meta file, must be skipped")
        con = sqlite3.connect(b / ".ibackup" / "index.db")
        con.execute("CREATE TABLE media_index(path TEXT PRIMARY KEY, size_bytes INTEGER, "
                    "mtime REAL, hash TEXT, gps_lat REAL, gps_lon REAL, date_src TEXT)")
        rows = [("E:\\POZE\\good.jpg", len(self.good), 1.0, h(self.good)),
                ("E:\\POZE\\bad.jpg", len(self.orig_bad), 1.0, h(self.orig_bad)),
                ("E:\\POZE\\gone.jpg", 7, 1.0, h(b"vanished")),
                ("E:\\POZE\\dup.jpg", len(self.dup), 1.0, h(self.dup))]
        con.executemany("INSERT INTO media_index(path,size_bytes,mtime,hash) VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()
        write(self.source / "present.jpg", self.good)
        write(self.source / "sub" / "missing.webp", b"webp bytes")
        write(self.source / "missing.aae", b"aae bytes")
        for folder in (self.backup, self.source):
            for p in folder.rglob("*"):
                if p.is_file():
                    os.utime(p, (1_500_000_000, 1_500_000_000))

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = verify_backup.main(argv, out_dir=self.out_dir)
        return code, buf.getvalue()

    def report(self, suffix=".log") -> str:
        files = sorted(self.out_dir.glob(f"*{suffix}"))
        self.assertEqual(len(files), 1, files)
        return files[0].read_text(encoding="utf-8")

    def test_backup_mode(self):
        before = (snapshot(self.backup), snapshot(self.source))
        code, console = self.run_main(["backup", str(self.backup)])
        self.assertEqual(code, 0, console)
        self.assertEqual(before, (snapshot(self.backup), snapshot(self.source)))
        text = self.report()
        self.assertIn("used: sha256", text)
        self.assertIn("files found: 6   hashed: 6", text)
        self.assertIn("matched an index hash: 3", text)       # good + dup1 + dup2
        self.assertIn("NOT in the index: 3", text)            # bad, extra, zero
        self.assertIn("index rows whose hash matches no file in the backup: 2", text)
        self.assertIn("zero-byte files: 1", text)
        self.assertIn("groups of identical files (non-empty): 1", text)
        self.assertIn("read errors: 0", text)
        self.assertIn("bad.jpg", text)
        self.assertIn("extra.jpg", text)
        self.assertIn("E:\\POZE\\gone.jpg", text)             # orphan row with source path
        self.assertIn("E:\\POZE\\bad.jpg", text)              # its content changed
        self.assertIn("\u03a0\u03b1\u03c4\u03c1\u03b1", text)
        self.assertIn("<10 MB", text)
        self.assertNotIn("ignored.txt", text)
        self.assertIn("dup1.jpg", text)
        self.assertIn("dup2.jpg", text)
        self.assertNotIn("PARTIAL", text)

        tsv = self.report(".tsv").splitlines()
        self.assertEqual(tsv[0], "#algo=sha256")
        self.assertEqual(tsv[3], "relpath\tsize\tmtime\thash\tmatched")
        rows = {r.split("\t")[0]: r.split("\t") for r in tsv[4:]}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[os.path.join("2019", "07_July", "good.jpg")][4], "yes")
        self.assertEqual(rows[os.path.join("2019", "07_July", "bad.jpg")][4], "no")
        self.assertEqual(rows[os.path.join("2019", "07_July", "good.jpg")][3], sha(self.good))

    def test_blake3_missing_is_reported(self):
        with mock.patch.object(verify_backup, "blake3_available", return_value=False):
            code, console = self.run_main(["backup", str(self.backup)])
            self.assertEqual(code, 0)
            self.assertIn("pip install blake3", console)
            self.assertIn("pip install blake3", self.report())
        for f in self.out_dir.glob("*"):
            f.unlink()
        with mock.patch.object(verify_backup, "blake3_available", return_value=False):
            code, console = self.run_main(["backup", str(self.backup), "--algo", "blake3"])
        self.assertEqual(code, 2)
        self.assertIn("pip install blake3", self.report())

    def test_source_mode(self):
        self.run_main(["backup", str(self.backup)])
        tsv = next(self.out_dir.glob("*.tsv"))
        before = (snapshot(self.backup), snapshot(self.source))
        code, console = self.run_main(["source", str(self.backup), str(self.source),
                                       "--hashes", str(tsv)])
        self.assertEqual(code, 0, console)
        self.assertEqual(before, (snapshot(self.backup), snapshot(self.source)))
        text = next(p for p in self.out_dir.glob("verify-source-*.log")).read_text(encoding="utf-8")
        self.assertIn("files hashed: 3 of 3", text)
        self.assertIn("present in the backup (a file with the same content is in it): 1", text)
        self.assertIn("MISSING from the backup: 2", text)
        self.assertIn("missing.webp", text)
        self.assertIn("missing.aae", text)
        self.assertIn(".webp", text)
        self.assertRegex(text, r"\.webp\s+0\s+1\s+photo\s\*")
        self.assertRegex(text, r"\.aae\s+0\s+1\s+sidecar\s\*")
        self.assertNotIn("present.jpg", text.split("missing files by extension")[1])

    def test_source_mode_uses_tsv_only_hashes(self):
        # content present in the backup but not in the index (extra.jpg) is found via the TSV
        write(self.source / "extra_copy.jpg", b"not in the index")
        self.run_main(["backup", str(self.backup)])
        tsv = next(self.out_dir.glob("*.tsv"))
        self.run_main(["source", str(self.backup), str(self.source), "--hashes", str(tsv)])
        text = next(p for p in self.out_dir.glob("verify-source-*.log")).read_text(encoding="utf-8")
        self.assertIn("present in the backup (a file with the same content is in it): 2", text)

    def test_a_file_known_only_to_the_old_index_is_missing(self):
        # Like a HEIC the old tool converted to JPG: the index has its hash, the backup has not.
        write(self.source / "IMG_0001.HEIC", self.orig_bad)
        self.run_main(["backup", str(self.backup)])
        tsv = next(self.out_dir.glob("*.tsv"))
        self.run_main(["source", str(self.backup), str(self.source), "--hashes", str(tsv)])
        text = next(p for p in self.out_dir.glob("verify-source-*.log")).read_text(encoding="utf-8")
        self.assertIn("MISSING from the backup: 3", text)
        self.assertIn("changed or deleted later): 1", text)
        self.assertIn("IMG_0001.HEIC  [old index only]", text)

    def source_with(self, tsv_lines):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tsv = self.out_dir / "given.tsv"
        tsv.write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")
        return self.run_main(["source", str(self.backup), str(self.source), "--hashes", str(tsv)])

    def test_source_mode_needs_a_tsv(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            verify_backup.main(["source", str(self.backup), str(self.source)], out_dir=self.out_dir)
        self.assertEqual(ctx.exception.code, 2)

    def good_row(self, mtime=1_500_000_000):
        return (f"{os.path.join('2019', '07_July', 'good.jpg')}\t{len(self.good)}\t{mtime}\t"
                f"{sha(self.good)}\tyes")

    def source_log(self):
        return next(p for p in self.out_dir.glob("verify-source-*.log")).read_text(encoding="utf-8")

    def test_a_tsv_from_an_interrupted_run_is_refused(self):
        code, console = self.source_with(["#algo=sha256", f"#backup={self.backup}", "#complete=no",
                                          "relpath\tsize\tmtime\thash\tmatched", self.good_row()])
        self.assertEqual(code, 2)
        self.assertIn("interrupted", console)

    def test_a_tsv_without_rows_is_refused(self):
        code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched"])
        self.assertEqual(code, 2)
        self.assertIn("no hash rows", console)

    def test_a_tsv_whose_files_are_not_in_this_backup_is_refused(self):
        code, console = self.source_with([
            "#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
            f"elsewhere\\present.jpg\t{len(self.good)}\t0\t{sha(self.good)}\tyes"])
        self.assertEqual(code, 2)
        self.assertIn("does not describe this backup", console)

    def test_a_tsv_made_under_another_drive_letter_is_used_with_a_note(self):
        code, console = self.source_with(["#algo=sha256", "#backup=E:\\POZE\\BACKUP_FINAL",
                                          "#complete=yes", "relpath\tsize\tmtime\thash\tmatched",
                                          self.good_row()])
        self.assertEqual(code, 0, console)
        self.assertIn("was made for E:\\POZE\\BACKUP_FINAL", self.source_log())
        self.assertIn("present in the backup (a file with the same content is in it): 1",
                      self.source_log())

    def test_a_row_whose_file_left_the_backup_is_not_used(self):
        removed = b"was in the backup when the TSV was made"
        write(self.source / "later_deleted.jpg", removed)
        code, console = self.source_with([
            "#algo=sha256", f"#backup={self.backup}", "#complete=yes",
            "relpath\tsize\tmtime\thash\tmatched", self.good_row(),
            f"2020\\gone.jpg\t{len(removed)}\t0\t{sha(removed)}\tno"])
        self.assertEqual(code, 0, console)
        text = self.source_log()
        self.assertIn("1 TSV rows no longer match", text)
        self.assertIn("later_deleted.jpg", text.split("missing files by extension")[1])

    def test_unreadable_source_files_are_counted_as_not_checked(self):
        real = verify_backup.hash_file

        def flaky(path, algos, progress):
            if path.endswith("present.jpg"):
                raise OSError("device busy")
            return real(path, algos, progress)

        with mock.patch.object(verify_backup, "hash_file", flaky):
            code, console = self.source_with(["#algo=sha256", f"#backup={self.backup}",
                                              "#complete=yes", "relpath\tsize\tmtime\thash\tmatched",
                                              self.good_row()])
        self.assertEqual(code, 0, console)
        self.assertIn("1 files or folders not checked (count them as missing)", console)
        self.assertIn("NOT CHECKED (unreadable files or folders, or interrupted; treat as NOT "
                      "backed up, see the errors section): 1",
                      self.source_log())

    def test_a_relative_tsv_path_works(self):
        self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched", self.good_row()])
        for f in self.out_dir.glob("verify-source-*"):
            f.unlink()
        cwd = os.getcwd()
        os.chdir(self.out_dir)
        try:
            code, console = self.run_main(["source", str(self.backup), str(self.source),
                                           "--hashes", "given.tsv"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, console)
        self.assertIn("present in the backup (a file with the same content is in it): 1",
                      self.source_log())

    def test_an_older_tsv_is_used_with_a_warning(self):
        code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
                                          self.good_row()])
        self.assertEqual(code, 0, console)
        self.assertIn("earlier version", console)
        text = self.source_log()
        self.assertIn("earlier version", text)
        self.assertIn("present in the backup (a file with the same content is in it): 1", text)

    def test_a_hash_like_file_name_is_not_read_as_a_header(self):
        code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
                                          self.good_row(), "#algo=bogus\t1\t0\tff\tno"])
        self.assertEqual(code, 0, console)
        self.assertIn("used: sha256", self.source_log())
        self.assertIn("1 TSV rows no longer match", self.source_log())

    def test_a_row_whose_file_was_rewritten_is_not_used(self):
        code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
                                          self.good_row(), self.good_row(mtime=1_400_000_000)])
        self.assertEqual(code, 0, console)
        self.assertIn("1 TSV rows no longer match", self.source_log())

    def test_more_than_half_the_rows_missing_is_refused(self):
        stale = [f"old\\f{n}.jpg\t10\t0\t{n:064x}\tno" for n in range(2)]
        code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
                                          self.good_row(), *stale])
        self.assertEqual(code, 2)
        self.assertIn("only 1 of 3 files", console)

    def test_a_source_that_overlaps_the_backup_is_refused(self):
        elsewhere = TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        for source in (self.backup.parent, self.backup / "2019"):
            with self.subTest(source=source):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = verify_backup.main(["source", str(self.backup), str(source),
                                               "--hashes", "x.tsv"], out_dir=elsewhere.name)
                self.assertEqual(code, 2)
                self.assertIn("overlaps the backup", err.getvalue())

    def test_an_unlistable_source_folder_counts_as_not_checked(self):
        real_walk = os.walk

        def walk_with_a_locked_folder(top, onerror=None, **kwargs):
            onerror(PermissionError(13, "Access is denied", os.path.join(top, "locked")))
            yield from real_walk(top, onerror=onerror, **kwargs)

        with mock.patch.object(verify_backup.os, "walk", walk_with_a_locked_folder):
            code, console = self.source_with(["#algo=sha256", "relpath\tsize\tmtime\thash\tmatched",
                                              self.good_row()])
        self.assertEqual(code, 0, console)
        self.assertIn("1 files or folders not checked (count them as missing)", console)
        self.assertIn("NOT CHECKED (unreadable files or folders", self.source_log())

    def test_backup_mode_records_its_folder_and_completion_in_the_tsv(self):
        self.run_main(["backup", str(self.backup)])
        head = next(self.out_dir.glob("*.tsv")).read_text(encoding="utf-8").splitlines()[:3]
        self.assertEqual(head, ["#algo=sha256", f"#backup={self.backup}", "#complete=yes"])

    def test_refuses_output_inside_inspected_folder(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = verify_backup.main(["backup", str(self.backup)], out_dir=self.backup / "out")
        self.assertEqual(code, 2)
        self.assertFalse((self.backup / "out").exists())

    def test_interrupt_writes_partial_report(self):
        real = verify_backup.hash_file
        calls = {"n": 0}

        def flaky(path, algos, progress):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real(path, algos, progress)

        with mock.patch.object(verify_backup, "hash_file", flaky):
            code, _ = self.run_main(["backup", str(self.backup)])
        self.assertEqual(code, 130)
        text = next(self.out_dir.glob("*PARTIAL.log")).read_text(encoding="utf-8")
        self.assertIn("PARTIAL REPORT", text)
        self.assertIn("hashed: 2", text)

    def test_read_error_does_not_stop_walk(self):
        real = verify_backup.hash_file

        def flaky(path, algos, progress):
            if path.endswith("extra.jpg"):
                raise PermissionError("denied on purpose")
            return real(path, algos, progress)

        with mock.patch.object(verify_backup, "hash_file", flaky):
            code, _ = self.run_main(["backup", str(self.backup)])
        self.assertEqual(code, 0)
        text = self.report()
        self.assertIn("read errors: 1", text)
        self.assertIn("denied on purpose", text)
        self.assertIn("hashed: 5", text)


@unittest.skipUnless(HAVE_BLAKE3, "blake3 is not installed")
class VerifyBackupBlake3Tests(VerifyBackupTests):
    hash_fn = staticmethod(b3)

    def test_backup_mode(self):
        before = snapshot(self.backup)
        code, _ = self.run_main(["backup", str(self.backup)])
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.backup))
        text = self.report()
        self.assertIn("used: blake3", text)
        self.assertIn("matched an index hash: 3", text)
        self.assertEqual(self.report(".tsv").splitlines()[0], "#algo=blake3")

    def test_blake3_missing_is_reported(self):
        pass

    def test_source_mode_uses_tsv_only_hashes(self):
        pass


class Blake3InstallPromptTests(unittest.TestCase):
    def offer(self, *, tty=True, answer="y", installed_after=True, returncode=0):
        run = mock.Mock(return_value=mock.Mock(returncode=returncode))
        with mock.patch.object(verify_backup.sys.stdin, "isatty", return_value=tty), \
                mock.patch("builtins.input", return_value=answer) as ask, \
                mock.patch.object(verify_backup.subprocess, "run", run), \
                mock.patch.object(verify_backup, "blake3_available",
                                  return_value=installed_after):
            result = verify_backup.offer_blake3_install()
        return result, ask, run

    def test_yes_runs_pip_for_this_python(self):
        result, _, run = self.offer(answer="y")
        self.assertTrue(result)
        run.assert_called_once_with(
            [verify_backup.sys.executable, "-m", "pip", "install", "blake3"])

    def test_anything_but_yes_installs_nothing(self):
        for answer in ("", "n", "no", "nu"):
            with self.subTest(answer=answer):
                result, _, run = self.offer(answer=answer)
                self.assertFalse(result)
                run.assert_not_called()

    def test_no_console_means_no_question(self):
        result, ask, run = self.offer(tty=False)
        self.assertFalse(result)
        ask.assert_not_called()
        run.assert_not_called()

    def test_a_failed_install_falls_back(self):
        result, _, _ = self.offer(returncode=1)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
