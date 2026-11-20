"""tools/inspect_backup.py: report content, and that it never touches the target."""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tests import synth

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "inspect_backup.py"
spec = importlib.util.spec_from_file_location("inspect_backup", SCRIPT)
inspect_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect_backup)


def snapshot(root: Path) -> dict:
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return snap


class InspectBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(self.tmp.name)
        self.target = base / "my backup é"
        self.out_dir = base / "out"
        t = self.target
        synth.write_photo(t / "2019" / "07 Barcelona" / "a.jpg",
                          taken_at=datetime(2019, 7, 12, 18, 30, 42))
        synth.write_photo(t / "2019" / "07 Barcelona" / "b.JPG")
        (t / "2019" / "empty.txt").write_bytes(b"")
        (t / "2020").mkdir()
        (t / "2020" / "c.jpg.part").write_bytes(b"xx")
        (t / ".backup" / "logs").mkdir(parents=True)
        con = sqlite3.connect(t / ".backup" / "index.db")
        con.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY, path TEXT)")
        con.execute("INSERT INTO photos (path) VALUES (?)", ("x" * 300,))
        con.commit()
        con.close()
        (t / ".backup" / "logs" / "x.log").write_text(
            "\n".join(f"line {n}" for n in range(100)), encoding="utf-8")
        old = datetime(2020, 1, 1).timestamp()
        for p in t.rglob("*"):
            if p.is_file():
                os.utime(p, (old, old))

    def tearDown(self):
        self.tmp.cleanup()

    def run_inspect(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = inspect_backup.main([str(self.target)], out_dir=self.out_dir)
        return code, buf.getvalue()

    def test_report_sections_and_target_untouched(self):
        before = snapshot(self.target)
        dirs_before = sorted(p.name for p in self.target.rglob("*") if p.is_dir())
        code, stdout = self.run_inspect()
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.target))
        self.assertEqual(dirs_before, sorted(p.name for p in self.target.rglob("*") if p.is_dir()))

        reports = list(self.out_dir.glob("inspect-my backup é-*.log"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(stdout.strip().splitlines()), 2)
        self.assertIn(reports[0].name, stdout)
        text = reports[0].read_text(encoding="utf-8")

        self.assertIn("files: 6\n", text)
        self.assertIn("0-byte files: 1", text)
        self.assertIn("empty.txt", text)
        self.assertIn("files ending in .part / .tmp / ~: 1", text)
        self.assertIn("starts with '.', Thumbs.db, desktop.ini): 0", text)
        self.assertRegex(text, r"\.jpg\s+2\s+\S+ \S+\s+photo")
        self.assertRegex(text, r"\.part\s+1\s+\S+ \S+\s+-")
        self.assertIn("07 Barcelona  direct=2  total=2", text)
        self.assertIn("distinct depth-1 folder names", text)
        self.assertIn("index.db", text)
        self.assertIn("rows: 1", text)
        self.assertIn("CREATE TABLE photos", text)
        self.assertIn("x" * 120 + "...(+180)", text)
        self.assertIn("x.log", text)
        self.assertIn("| line 0\n", text)
        self.assertIn("| line 99\n", text)
        self.assertIn("errors: 0", text)

    def test_a_wal_database_is_read_without_touching_its_files(self):
        db = self.target / ".backup" / "live.db"
        writer = sqlite3.connect(db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE media (id INTEGER PRIMARY KEY)")
            writer.executemany("INSERT INTO media VALUES (?)", [(n,) for n in range(7)])
            writer.commit()
            self.assertTrue(Path(str(db) + "-wal").exists())
            before = snapshot(self.target)
            code, _ = self.run_inspect()
            self.assertEqual(code, 0)
            self.assertEqual(before, snapshot(self.target))
        finally:
            writer.close()
        text = next(self.out_dir.glob("inspect-*.log")).read_text(encoding="utf-8")
        self.assertIn("rows: 7", text)

    def test_refuses_output_inside_target(self):
        before = snapshot(self.target)
        code = inspect_backup.main([str(self.target)], out_dir=self.target / "out")
        self.assertEqual(code, 2)
        self.assertEqual(before, snapshot(self.target))

    def test_folder_without_meta_dir(self):
        plain = Path(self.tmp.name) / "plain"
        synth.write_photo(plain / "a.jpg")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(inspect_backup.main([str(plain)], out_dir=self.out_dir), 0)
        text = next(self.out_dir.glob("inspect-plain-*.log")).read_text(encoding="utf-8")
        self.assertIn("none of .backup", text)


if __name__ == "__main__":
    unittest.main()
