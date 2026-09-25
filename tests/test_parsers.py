"""Teste pentru citirea metadatelor din JPEG, HEIC si MOV."""
from __future__ import annotations

import io
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ibackup import exif, isobmff, mediameta
from tests import synth

BUCURESTI = (44.4268, 26.1025)


class ExifTests(unittest.TestCase):
    def test_datetime_and_gps_roundtrip(self):
        taken = datetime(2019, 7, 12, 18, 30, 42)
        block = synth.build_exif(taken, BUCURESTI)
        tags = exif.parse_tiff(block)

        dt, source = exif.exif_datetime(tags)
        self.assertEqual(dt, taken)
        self.assertEqual(source, "exif:DateTimeOriginal")

        coords = exif.exif_gps(tags)
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], BUCURESTI[0], places=3)
        self.assertAlmostEqual(coords[1], BUCURESTI[1], places=3)

        make, model = exif.exif_camera(tags)
        self.assertEqual(make, "Apple")
        self.assertEqual(model, "iPhone 13")

    def test_southern_and_western_hemisphere(self):
        block = synth.build_exif(datetime(2020, 1, 1), (-33.87, -70.67))
        coords = exif.exif_gps(exif.parse_tiff(block))
        self.assertLess(coords[0], 0)
        self.assertLess(coords[1], 0)

    def test_zero_gps_is_ignored(self):
        block = synth.build_exif(datetime(2020, 1, 1), (0.0, 0.0))
        self.assertIsNone(exif.exif_gps(exif.parse_tiff(block)))

    def test_jpeg_segment_extraction(self):
        block = synth.build_exif(datetime(2021, 3, 4, 5, 6, 7), BUCURESTI)
        data = synth.build_jpeg(block)
        found = exif.exif_from_jpeg(data)
        self.assertEqual(found, block)

    def test_jpeg_without_exif(self):
        self.assertIsNone(exif.exif_from_jpeg(synth.build_jpeg(None)))

    def test_garbage_does_not_raise(self):
        self.assertEqual(exif.parse_tiff(b"nu sunt tiff"), {"ifd0": {}, "exif": {}, "gps": {}})
        self.assertIsNone(exif.exif_from_jpeg(b"\xff\xd8\xff\xe1\x00\x02"))
        self.assertEqual(exif.exif_datetime({"exif": {}, "ifd0": {}}), (None, ""))


class HeifTests(unittest.TestCase):
    def test_exif_item_is_found(self):
        block = synth.build_exif(datetime(2022, 8, 9, 10, 11, 12), BUCURESTI)
        data = synth.build_heic(block)
        fh = io.BytesIO(data)
        self.assertEqual(isobmff.container_kind(fh, len(data)), "heif")
        extracted = isobmff.heif_exif_block(fh, len(data))
        self.assertEqual(extracted, block)

        tags = exif.parse_tiff(extracted)
        dt, _ = exif.exif_datetime(tags)
        self.assertEqual(dt, datetime(2022, 8, 9, 10, 11, 12))

    def test_truncated_file_returns_none(self):
        block = synth.build_exif(datetime(2022, 1, 1), None)
        data = synth.build_heic(block)[:40]
        fh = io.BytesIO(data)
        self.assertIsNone(isobmff.heif_exif_block(fh, len(data)))


class QuickTimeTests(unittest.TestCase):
    def test_day_and_xyz_atoms(self):
        data = synth.build_mov()
        fh = io.BytesIO(data)
        self.assertEqual(isobmff.container_kind(fh, len(data)), "quicktime")
        info = isobmff.quicktime_metadata(fh, len(data))
        self.assertEqual(info["taken_at_source"], "qt:day")
        # Offset-ul +03:00 e ora locala de la fata locului, o pastram ca atare.
        self.assertEqual(info["taken_at"].hour, 18)
        self.assertAlmostEqual(info["gps"][0], 44.4268, places=4)
        self.assertAlmostEqual(info["gps"][1], 26.1025, places=4)

    def test_falls_back_to_mvhd(self):
        # 3 600 000 000 s dupa 1904-01-01 = 2018-02-11T15:20:00Z
        data = synth.build_mov(created=None, iso6709=None, mvhd_seconds=3600000000)
        fh = io.BytesIO(data)
        info = isobmff.quicktime_metadata(fh, len(data))
        self.assertEqual(info["taken_at_source"], "qt:mvhd")
        self.assertEqual(info["taken_at"].year, 2018)

    def test_iso6709_parsing(self):
        self.assertIsNone(isobmff.parse_iso6709(""))
        self.assertIsNone(isobmff.parse_iso6709("+00.0000+000.0000/"))
        lat, lon = isobmff.parse_iso6709("-33.8688+151.2093/")
        self.assertAlmostEqual(lat, -33.8688, places=4)
        self.assertAlmostEqual(lon, 151.2093, places=4)


class MediaMetaTests(unittest.TestCase):
    def test_filename_patterns(self):
        cases = {
            "IMG_20190712_183042.jpg": datetime(2019, 7, 12, 18, 30, 42),
            "20190712_183042.jpg": datetime(2019, 7, 12, 18, 30, 42),
            "IMG-20190712-WA0001.jpg": datetime(2019, 7, 12),
            "Screenshot 2023-05-01 at 12.33.44.png": datetime(2023, 5, 1, 12, 33, 44),
            "2019-07-12 18.30.42.jpg": datetime(2019, 7, 12, 18, 30, 42),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(mediameta.datetime_from_name(name), expected)

    def test_filename_without_date(self):
        self.assertIsNone(mediameta.datetime_from_name("IMG_0042.HEIC"))
        self.assertIsNone(mediameta.datetime_from_name("vacanta.jpg"))

    def test_impossible_date_is_rejected(self):
        self.assertIsNone(mediameta.datetime_from_name("IMG_20191332_000000.jpg"))

    def test_extract_prefers_exif_over_filename(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_20200101_000000.jpg"
            synth.write_photo(path, datetime(2019, 7, 12, 18, 30, 42), BUCURESTI)
            meta = mediameta.extract(path)
            self.assertEqual(meta.taken_at, datetime(2019, 7, 12, 18, 30, 42))
            self.assertEqual(meta.taken_at_source, "exif:DateTimeOriginal")
            self.assertTrue(meta.has_gps)
            self.assertEqual(meta.kind, "photo")

    def test_extract_falls_back_to_filename_then_mtime(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_20200103_101112.jpg"
            path.write_bytes(synth.build_jpeg(None))
            meta = mediameta.extract(path)
            self.assertEqual(meta.taken_at, datetime(2020, 1, 3, 10, 11, 12))
            self.assertEqual(meta.taken_at_source, "filename")

            plain = Path(tmp) / "poza.jpg"
            plain.write_bytes(synth.build_jpeg(None))
            meta = mediameta.extract(plain)
            self.assertEqual(meta.taken_at_source, "mtime")
            self.assertIsNotNone(meta.taken_at)

    def test_heic_file_is_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_0001.HEIC"
            block = synth.build_exif(datetime(2024, 6, 1, 9, 0, 0), BUCURESTI)
            path.write_bytes(synth.build_heic(block))
            meta = mediameta.extract(path)
            self.assertEqual(meta.taken_at, datetime(2024, 6, 1, 9, 0, 0))
            self.assertTrue(meta.has_gps)

    def test_mov_file_is_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_0001.MOV"
            path.write_bytes(synth.build_mov())
            meta = mediameta.extract(path)
            self.assertEqual(meta.kind, "video")
            self.assertEqual(meta.taken_at, datetime(2019, 7, 12, 18, 30, 42))
            self.assertTrue(meta.has_gps)

    def test_corrupt_file_does_not_raise(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "stricat.jpg"
            path.write_bytes(b"\xff\xd8" + b"\x00" * 100)
            meta = mediameta.extract(path)
            self.assertEqual(meta.taken_at_source, "mtime")


if __name__ == "__main__":
    unittest.main()
