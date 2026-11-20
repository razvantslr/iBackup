"""The command line, driven through cli.main() as a user would."""
from __future__ import annotations

import io
import sqlite3
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ibackup import __version__, cli, exif, pipeline
from tests import synth

BARCELONA = (41.3901, 2.1701)

# One row in the real GeoNames dump format (19 tab-separated columns).
GEONAMES_ROW = "\t".join([
    "681290", "Cluj-Napoca", "Cluj-Napoca", "Cluj", "46.76667", "23.6",
    "P", "PPLA", "RO", "", "13", "", "", "", "324576", "", "360",
    "Europe/Bucharest", "2023-01-01",
]) + "\n"


class GeonamesInstallTests(unittest.TestCase):
    def _install(self, dest: Path, source: Path) -> int:
        with redirect_stdout(io.StringIO()):
            return cli.main(["--dest", str(dest), "geonames",
                             "--install", str(source)])

    def test_install_from_txt_keeps_the_users_file(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "backup"
            mine = Path(tmp) / "cities5000.txt"
            mine.write_text(GEONAMES_ROW, encoding="utf-8")

            self.assertEqual(self._install(dest, mine), 0)

            self.assertTrue(mine.exists())
            self.assertIn("Cluj-Napoca",
                          (dest / ".ibackup" / cli.DATASET_NAME).read_text("utf-8"))

    def test_install_from_zip_keeps_the_zip_and_removes_the_extract(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "backup"
            mine = Path(tmp) / "cities5000.zip"
            with zipfile.ZipFile(mine, "w") as zf:
                zf.writestr("cities5000.txt", GEONAMES_ROW)

            self.assertEqual(self._install(dest, mine), 0)

            self.assertTrue(mine.exists())
            self.assertFalse((dest / ".ibackup" / "cities5000.txt").exists())
            self.assertTrue((dest / ".ibackup" / cli.DATASET_NAME).exists())


class RunLogTests(unittest.TestCase):
    """The log of a run is what gets read afterwards to check a real import."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.src = root / "old"
        self.dest = root / "backup"
        synth.write_photo(self.src / "trip" / "IMG_0001.jpg",
                          datetime(2025, 1, 14, 18, 30), BARCELONA)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, exit_code=None) -> str:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--dest", str(self.dest), *args])
        if exit_code is not None:
            self.assertEqual(code, exit_code)
        logs = sorted((self.dest / ".ibackup" / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1, logs)
        self.log_name = logs[0].name
        return logs[0].read_text(encoding="utf-8")

    def test_a_dry_run_writes_a_log(self):
        log = self.run_cli("scan", "--source", str(self.src))
        self.assertTrue(self.log_name.startswith("import-dryrun-"), self.log_name)
        self.assertIn("would copy: trip/IMG_0001.jpg -> "
                      "2025/01_ianuarie/Barcelona/IMG_0001.jpg", log)

    def test_the_log_says_where_date_and_place_came_from(self):
        log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("copied: trip/IMG_0001.jpg -> "
                      "2025/01_ianuarie/Barcelona/IMG_0001.jpg", log)
        self.assertIn("date 2025-01-14 18:30 (exif:DateTimeOriginal)", log)
        self.assertIn("place Barcelona (builtin)", log)

    def test_the_log_records_version_and_settings(self):
        log = self.run_cli("import", "--source", str(self.src))
        self.assertIn(f"ibackup {__version__}", log)
        self.assertIn("screenshot_dir='Screenshots'", log)

    def test_an_error_leaves_a_traceback_in_the_log(self):
        with patch.object(pipeline.Importer, "_ingest",
                          side_effect=RuntimeError("boom")):
            log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("RuntimeError: boom", log)
        self.assertIn("Traceback", log)

    def test_a_run_that_fails_to_start_leaves_a_traceback(self):
        with patch.object(cli, "build_source",
                          side_effect=RuntimeError("source gone")):
            log = self.run_cli("import", "--source", str(self.src), exit_code=2)
        self.assertIn("RuntimeError: source gone", log)
        self.assertIn("Traceback", log)

    def test_ctrl_c_is_written_to_the_log(self):
        with patch.object(pipeline.Importer, "_process_group",
                          side_effect=KeyboardInterrupt):
            log = self.run_cli("import", "--source", str(self.src), exit_code=130)
        self.assertIn("Interrupted by the user", log)

    def test_a_paired_file_takes_its_date_from_the_photo(self):
        live = self.src / "live"
        live.mkdir()
        (live / "IMG_0003.HEIC").write_bytes(synth.build_heic(
            synth.build_exif(datetime(2025, 1, 14, 18, 5), BARCELONA)))
        (live / "IMG_0003.MOV").write_bytes(
            synth.build_mov("2025-01-14T18:07:00+0100", "+41.3901+002.1701/"))
        log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("IMG_0003.MOV [video; date 2025-01-14 18:05 (its photo)", log)

    def test_skipped_and_non_media_files_are_reported(self):
        (self.src / "empty.jpg").write_bytes(b"")
        (self.src / "notes.txt").write_text("x", encoding="utf-8")
        log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("not read from the source:", log)
        self.assertIn("empty.jpg (empty file)", log)
        self.assertIn("Not media, left in the source: .txt x1", log)
        self.assertRegex(log, r"skipped in the source: unreadable or empty\s+1")

    def test_an_unreadable_folder_is_reported(self):
        def walk_with_a_locked_folder(top, onerror=None, followlinks=False):
            onerror(PermissionError(13, "Access is denied", str(Path(top) / "locked")))
            yield str(top), [], ["IMG_0001.jpg"]

        with patch("ibackup.sources.folder.os.walk", walk_with_a_locked_folder):
            log = self.run_cli("scan", "--source", str(self.src / "trip"))
        self.assertIn("locked (folder not readable: Access is denied)", log)

    def test_verbose_keeps_tracebacks_off_the_console(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(pipeline.Importer, "_ingest",
                          side_effect=RuntimeError("boom")), \
                redirect_stdout(out), redirect_stderr(err):
            cli.main(["--dest", str(self.dest), "-v", "import", "--source", str(self.src)])
        self.assertIn("RuntimeError: boom", err.getvalue())
        self.assertNotIn("Traceback", out.getvalue() + err.getvalue())

    def test_unreadable_metadata_is_reported(self):
        with patch.object(exif, "parse_tiff", side_effect=ValueError("bad TIFF")):
            log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("metadata unreadable", log)
        self.assertIn("trip/IMG_0001.jpg: ValueError: bad TIFF", log)


def snapshot(root: Path):
    """Every file as (size, mtime_ns) plus the set of directories."""
    files, dirs = {}, set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            dirs.add(rel)
        else:
            st = path.stat()
            files[rel] = (st.st_size, st.st_mtime_ns)
    return files, dirs


class ForeignIndexTests(unittest.TestCase):
    """A backup made by an older tool keeps its own index.db; never touch it."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.src = root / "old"
        self.dest = root / "backup"
        synth.write_photo(self.src / "IMG_0001.jpg",
                          datetime(2025, 1, 14, 18, 30), BARCELONA)
        synth.write_photo(self.dest / "2025" / "IMG_0002.jpg",
                          datetime(2025, 2, 1, 9, 0), BARCELONA)
        synth.write_photo(self.dest / "2025" / "IMG_0003.jpg",
                          datetime(2025, 2, 2, 9, 0), BARCELONA)
        self.index = self.dest / ".ibackup" / "index.db"
        self.index.parent.mkdir(parents=True)
        (self.index.parent / "logs").mkdir()
        (self.index.parent / "logs" / "ibackup_2026-03-25.log").write_text(
            "old tool log\n", encoding="utf-8")
        self.cities = root / "cities5000.txt"
        self.cities.write_text(GEONAMES_ROW, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def make_foreign(self, wal=False):
        conn = sqlite3.connect(str(self.index))
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE media_index(path TEXT PRIMARY KEY, size_bytes INTEGER,
                mtime REAL, hash TEXT, gps_lat REAL, gps_lon REAL, date_src TEXT);
            CREATE TABLE location_cache(lat_r REAL, lon_r REAL, city TEXT,
                PRIMARY KEY(lat_r, lon_r));
            INSERT INTO media_index VALUES ('2025/IMG_0002.jpg', 10, 1.5, 'aa',
                41.39, 2.17, 'exif');
            INSERT INTO media_index VALUES ('2025/IMG_0003.jpg', 11, 2.5, 'bb',
                NULL, NULL, 'mtime');
            INSERT INTO location_cache VALUES (41.39, 2.17, 'Barcelona');
        """)
        conn.commit()
        return conn

    def commands(self):
        return {
            "init": ["init"],
            "import": ["import", "--source", str(self.src)],
            "scan": ["scan", "--source", str(self.src)],
            "status": ["status"],
            "verify": ["verify"],
            "relocate": ["relocate"],
            "reorganize": ["reorganize"],
            "regeocode": ["regeocode"],
            "reindex": ["reindex"],
            "geonames": ["geonames", "--install", str(self.cities)],
        }

    def assert_all_refused(self):
        before = snapshot(self.dest)
        for name, args in self.commands().items():
            with self.subTest(command=name):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["--dest", str(self.dest), *args])
                self.assertEqual(code, 2)
                self.assertIn(str(self.index), err.getvalue())
                self.assertIn("another tool", err.getvalue())
                self.assertEqual(snapshot(self.dest), before)

    def test_every_command_refuses_a_foreign_index(self):
        self.make_foreign().close()
        self.assert_all_refused()

    def test_a_foreign_wal_index_with_an_open_writer_is_left_alone(self):
        writer = self.make_foreign(wal=True)
        try:
            writer.execute("INSERT INTO location_cache VALUES (1.0, 2.0, 'X')")
            writer.commit()
            self.assertTrue(self.index.with_name("index.db-wal").exists())
            self.assertTrue(self.index.with_name("index.db-shm").exists())
            self.assert_all_refused()
        finally:
            writer.close()

    def test_a_foreign_table_that_only_lives_in_the_wal_is_detected(self):
        writer = sqlite3.connect(str(self.index))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE media_index(path TEXT PRIMARY KEY)")
        writer.commit()
        try:
            self.assert_all_refused()
        finally:
            writer.close()

    def test_a_fresh_dest_and_an_existing_ibackup_dest_still_work(self):
        fresh = Path(self._tmp.name) / "fresh"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            first = cli.main(["--dest", str(fresh), "import", "--source", str(self.src)])
            second = cli.main(["--dest", str(fresh), "import", "--source", str(self.src)])
        self.assertEqual((first, second), (0, 0))
        self.assertTrue((fresh / ".ibackup" / "index.db").exists())

    def test_an_empty_index_file_counts_as_fresh(self):
        self.index.write_bytes(b"")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--dest", str(self.dest), "import",
                             "--source", str(self.src)])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
