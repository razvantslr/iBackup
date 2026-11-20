"""The command line, driven through cli.main() as a user would."""
from __future__ import annotations

import io
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from ibackup import cli

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


if __name__ == "__main__":
    unittest.main()
