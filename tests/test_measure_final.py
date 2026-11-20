"""tools/measure_final.py: JPEG settings, HEIC-to-JPG matching, and that it never writes."""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import struct
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tests import synth

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "measure_final.py"
spec = importlib.util.spec_from_file_location("measure_final", SCRIPT)
measure_final = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure_final)

try:
    from PIL import Image
except ImportError:
    Image = None


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def build_jpeg(taken_at=None, gps=None, make="Apple", luma=None, sampling=(0x22, 0x11, 0x11),
               icc=False, size=(4032, 3024)) -> bytes:
    """Markers only (no image data): EXIF, optional ICC, DQT, SOF0, SOS."""
    out = b"\xff\xd8"
    if taken_at or gps:
        out += segment(0xE1, b"Exif\x00\x00" + synth.build_exif(taken_at, gps, make, "iPhone 13"))
    if icc:
        out += segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"\x00" * 32)
    natural = luma if luma is not None else measure_final.ijg_table(85)
    zigzag = bytes(natural[measure_final.ZIGZAG[k]] for k in range(64))
    out += segment(0xDB, b"\x00" + zigzag + b"\x01" + zigzag)
    comps = b"".join(bytes([i + 1, s, 0 if i == 0 else 1]) for i, s in enumerate(sampling))
    out += segment(0xC0, b"\x08" + struct.pack(">HH", size[1], size[0]) + bytes([len(sampling)]) + comps)
    out += segment(0xDA, b"\x00") + b"\xff\xd9"
    return out


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_heic(path: Path, taken_at) -> None:
    write(path, synth.build_heic(synth.build_exif(taken_at)))


def snapshot(root: Path) -> dict:
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, p.read_bytes())
    return snap


D1 = datetime(2019, 7, 12, 18, 30, 42)
D2 = datetime(2019, 7, 13, 10, 0, 0)
D3 = datetime(2020, 1, 2, 9, 0, 0)
D4 = datetime(2022, 5, 1, 12, 0, 0)
D5 = datetime(2022, 5, 1, 12, 0, 1)
APPLE_LUMA = tuple(max(1, v // 3) for v in measure_final.STD_LUMA)  # not an IJG table


class QualityTests(unittest.TestCase):
    def test_ijg_tables_are_recognised_exactly(self):
        for q in (10, 50, 75, 85, 90, 95):
            self.assertEqual(measure_final.quality_label(measure_final.ijg_table(q)),
                             f"q{q} (IJG table, exact)")

    def test_other_tables_get_an_estimate(self):
        label = measure_final.quality_label(APPLE_LUMA)
        self.assertTrue(label.startswith("~q"), label)
        self.assertIn("estimate", label)

    def test_segments(self):
        info = measure_final.jpeg_segments(build_jpeg(D1, icc=True, sampling=(0x21, 0x11, 0x11)))
        self.assertEqual(info["luma"], measure_final.ijg_table(85))
        self.assertTrue(info["icc"])
        self.assertIsNotNone(info["exif"])
        self.assertEqual(info["size"], (4032, 3024))
        self.assertEqual(measure_final.subsampling(info["comps"]), "4:2:2")

    def test_not_a_jpeg(self):
        info = measure_final.jpeg_segments(b"not a jpeg at all")
        self.assertIsNone(info["luma"])
        self.assertEqual(measure_final.quality_label(info["luma"]), "no DQT found")

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_pillow_output_is_read_back_exactly(self):
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (200, 100, 50)).save(buf, "JPEG", quality=85)
        info = measure_final.jpeg_segments(buf.getvalue())
        self.assertEqual(measure_final.quality_label(info["luma"]), "q85 (IJG table, exact)")
        self.assertEqual(measure_final.subsampling(info["comps"]), "4:2:0")
        self.assertEqual(info["size"], (64, 48))


class MeasureFinalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(self.tmp.name)
        self.backup = base / "backup_final"
        self.heic = base / "salvare-backup"
        self.out = base / "out"
        b, h = self.backup, self.heic

        meta = b / ".ibackup"
        meta.mkdir(parents=True)
        conn = sqlite3.connect(meta / "index.db")
        conn.execute("CREATE TABLE media_index (path TEXT, size_bytes INTEGER, hash TEXT)")
        for n in range(1, 6):
            conn.execute("INSERT INTO media_index VALUES (?, ?, ?)",
                         (f"E:\\POZE\\_backup26\\IMG_000{n}.HEIC", 100, f"h{n}"))
        conn.commit()
        conn.close()

        write(b / "2019" / "07_July" / "Barcelona" / "IMG_0001.jpg", build_jpeg(D1, gps=(41.39, 2.17)))
        write(b / "2019" / "07_July" / "Barcelona" / "IMG_0002.jpg", build_jpeg(D2))
        write(b / "2020" / "01_January" / "a" / "IMG_0003.jpg", build_jpeg(D3))
        write(b / "2020" / "01_January" / "b" / "IMG_0003.jpg", build_jpeg(D3))
        write(b / "2022" / "05_May" / "x" / "IMG_0004.jpg", build_jpeg(D4))
        write(b / "2018" / "03_March" / "y" / "IMG_9000.JPG", build_jpeg(D1, luma=APPLE_LUMA, icc=True))
        write(b / "2017" / "01_January" / "z" / "DSC_1.jpg", build_jpeg(D1, make="NIKON"))

        write_heic(h / "IMG_0001.HEIC", D1)                # its JPG is in the backup
        write_heic(h / "IMG_E0002.HEIC", D2)               # edit, original IMG_0002.jpg present
        write_heic(h / "IMG_0003.HEIC", D3)                # two JPGs match
        write_heic(h / "IMG_0004.HEIC", D5)                # same stem, other date
        write_heic(h / "sub" / "IMG_0099.HEIC", D1)        # nothing in the backup
        write_heic(h / "IMG_E0098.HEIC", D4)               # edit, original missing
        (h / "IMG_0001.AAE").write_bytes(b"<plist/>")       # not a HEIC, ignored

        self.before = (snapshot(b), snapshot(h))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = measure_final.main(list(argv), out_dir=self.out)
        return code, buf.getvalue()

    def report(self) -> str:
        logs = list(self.out.glob("measure-backup_final-*.log"))
        self.assertEqual(len(logs), 1)
        return logs[0].read_text(encoding="utf-8")

    def test_full_run(self):
        code, printed = self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertEqual(code, 0, printed)
        self.assertIn("7 JPGs, 6 HEIC (1 with their JPG, 1 edits with their original), 0 errors", printed)
        text = self.report()

        self.assertIn("5 rows, 5 distinct HEIC name stems", text)
        self.assertIn("reuse an IMG_nnnn name): 5 files", text)
        self.assertIn("conversions the old index does not name): 1 files", text)
        self.assertIn("all other JPGs: 1 files", text)
        self.assertIn("q85 (IJG table, exact)", text)
        self.assertIn("~q", text)
        self.assertIn("ICC colour profile: 1 of 1", text)
        self.assertIn("GPS: 1 of 5", text)
        self.assertIn("EXIF date: exif:DateTimeOriginal 5", text)
        self.assertIn("4:2:0 5", text)

        self.assertIn("HEIC files: 6", text)
        self.assertRegex(text, r"\n\s+1  a JPG with the same name stem and the same EXIF date")
        self.assertRegex(text, r"\n\s+1  more than one JPG")
        self.assertRegex(text, r"\n\s+1  an edit \(IMG_E\.\.\.\).*IMG_E edits: 1")
        self.assertRegex(text, r"\n\s+1  a JPG with the same name stem \(for an edit")
        self.assertRegex(text, r"\n\s+2  no JPG with this name stem.*IMG_E edits: 1")
        self.assertIn(os.path.join("2019", "07_July", "Barcelona", "IMG_0002.jpg"), text)
        self.assertIn("EXIF date 2022-05-01 12:00:00", text)
        self.assertIn(os.path.join("sub", "IMG_0099.HEIC"), text)
        self.assertIn("errors: 0", text)

    def test_certain_conversions_group(self):
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        text = self.report()
        self.assertIn("so by the old tool): 1 files", text)

    def test_edit_whose_original_lost_its_date_shows_the_original(self):
        write(self.backup / "2023" / "01_January" / "w" / "IMG_0098.jpg", build_jpeg())
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        text = self.report()
        self.assertRegex(text, r"\n\s+1  no JPG with this name stem")
        self.assertIn(os.path.join("w", "IMG_0098.jpg") + " (EXIF date none)", text)

    def test_many_jpgs_with_one_stem_say_how_many_are_hidden(self):
        for n in range(8):
            write(self.backup / "2015" / f"e{n}" / "IMG_0004.jpg", build_jpeg(datetime(2015, 1, 1 + n)))
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertIn("... 4 more JPGs named img_0004", self.report())

    def test_gps_clock_date_is_not_used_as_exif_date(self):
        real = measure_final.extract

        def fake(path, **kw):
            meta = real(path, **kw)
            if Path(path).name == "IMG_0001.HEIC":
                meta.taken_at_source = "exif:GPSDateStamp"
            return meta
        with unittest.mock.patch.object(measure_final, "extract", fake):
            self.run_tool(str(self.backup), "--heic", str(self.heic))
        text = self.report()
        self.assertRegex(text, r"\n\s+0  a JPG with the same name stem and the same EXIF date")
        self.assertIn("no EXIF date  IMG_0001.HEIC", text)

    def test_broken_jpegs_are_errors(self):
        write(self.backup / "x" / "heic_renamed.jpg", synth.build_heic(synth.build_exif(D1)))
        write(self.backup / "x" / "empty.jpg", b"")
        write(self.backup / "x" / "no_tables.jpg", b"\xff\xd8" + segment(0xE0, b"JFIF\x00") + b"\xff\xd9")
        code, printed = self.run_tool(str(self.backup))
        self.assertEqual(code, 0)
        self.assertIn("10 JPGs", printed)
        self.assertIn("3 errors", printed)
        text = self.report()
        self.assertIn("heic_renamed.jpg: not a JPEG", text)
        self.assertIn("empty.jpg: not a JPEG", text)
        self.assertIn("no_tables.jpg: no quantization table", text)

    def test_ctrl_c_keeps_what_was_read(self):
        real = measure_final.extract
        calls = []

        def fake(path, **kw):
            calls.append(path)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real(path, **kw)
        with unittest.mock.patch.object(measure_final, "extract", fake):
            code, printed = self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertEqual(code, 130)
        self.assertIn("7 JPGs, 2 HEIC", printed)
        self.assertIn("PARTIAL", printed)
        text = self.report()
        self.assertIn("PARTIAL REPORT", text)
        self.assertIn("the HEIC phase had started", text)
        self.assertIn("JPGs read: 7", text)
        self.assertIn("HEIC files: 2", text)

    def test_copy_suffix_still_matches(self):
        write(self.backup / "2019" / "07_July" / "X" / "IMG_0099_2.jpg", build_jpeg(D1))
        write_heic(self.heic / "IMG_0004 (1).HEIC", D4)
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        text = self.report()
        self.assertRegex(text, r"\n\s+2  a JPG with the same EXIF date whose name differs only")
        self.assertIn(os.path.join("X", "IMG_0099_2.jpg"), text)
        self.assertRegex(text, r"\n\s+1  no JPG with this name stem")

    def test_damaged_heic_is_an_error(self):
        write(self.heic / "IMG_5000.HEIC", bytes(4096))
        code, printed = self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertEqual(code, 0)
        self.assertIn("1 errors", printed)
        self.assertIn("IMG_5000.HEIC: not a HEIF container", self.report())

    def test_duplicate_heics_count_their_jpg_once(self):
        write_heic(self.heic / "copy" / "IMG_0001.HEIC", D1)
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        text = self.report()
        self.assertRegex(text, r"\n\s+2  a JPG with the same name stem and the same EXIF date")
        self.assertIn("so by the old tool): 1 files", text)

    def test_unexpected_error_still_writes_a_partial_report(self):
        with unittest.mock.patch.object(measure_final, "match_heics", side_effect=PermissionError("busy")):
            code, printed = self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertEqual(code, 130)
        text = self.report()
        self.assertIn("PARTIAL REPORT: stopped by an error (PermissionError: busy)", text)
        self.assertIn("JPGs read: 7", text)

    def test_nothing_is_written_into_either_folder(self):
        self.run_tool(str(self.backup), "--heic", str(self.heic))
        self.assertEqual((snapshot(self.backup), snapshot(self.heic)), self.before)
        self.assertFalse((self.backup / ".ibackup" / "index.db-journal").exists())

    def test_without_index_or_heic_folder(self):
        (self.backup / ".ibackup" / "index.db").unlink()
        code, _ = self.run_tool(str(self.backup))
        self.assertEqual(code, 0)
        text = self.report()
        self.assertIn("no old index", text)
        self.assertNotIn("likely conversions):", text)
        self.assertIn("not run (no --heic folder given)", text)

    def test_refuses_report_inside_a_measured_folder(self):
        for target in (self.backup / "reports", self.heic):
            buf = io.StringIO()
            with redirect_stdout(buf), unittest.mock.patch("sys.stderr", io.StringIO()):
                code = measure_final.main([str(self.backup), "--heic", str(self.heic)], out_dir=target)
            self.assertEqual(code, 2)
        self.assertEqual((snapshot(self.backup), snapshot(self.heic)), self.before)

    def test_missing_folder(self):
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            code = measure_final.main([str(self.backup / "nope")], out_dir=self.out)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
