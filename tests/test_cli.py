"""The command line, driven through cli.main() as a user would."""
from __future__ import annotations

import io
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ibackup import cli, pipeline
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

    def run_cli(self, *args) -> str:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(["--dest", str(self.dest), *args])
        logs = sorted((self.dest / ".ibackup" / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1, logs)
        return logs[0].read_text(encoding="utf-8")

    def test_a_dry_run_writes_a_log(self):
        log = self.run_cli("scan", "--source", str(self.src))
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
        self.assertIn("ibackup 0.2.0", log)
        self.assertIn("screenshot_dir='Screenshots'", log)

    def test_an_error_leaves_a_traceback_in_the_log(self):
        with patch.object(pipeline.Importer, "_ingest",
                          side_effect=RuntimeError("boom")):
            log = self.run_cli("import", "--source", str(self.src))
        self.assertIn("RuntimeError: boom", log)
        self.assertIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
