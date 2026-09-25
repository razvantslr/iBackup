"""Tests for screenshot classification and where screenshots are filed."""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ibackup import mediameta, organize, screenshot
from ibackup.config import Config
from tests import synth

BUCURESTI = (44.4268, 26.1025)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def kind_of(self, path) -> str:
        return mediameta.extract(path).kind

    # --- caught by the name ------------------------------------------------

    def test_filename_variants(self):
        names = [
            "Screenshot 2023-05-01 at 12.33.44.png",
            "Screen Shot 2019-01-01.png",
            "screen_shot.png",
            "screen-shot-2020.png",
            "Screenshot_20210102-113000.png",
            "captura-de-ecran.png",
        ]
        for name in names:
            with self.subTest(name=name):
                path = synth.write_png(self.root / name, make="Apple",
                                       model="iPhone 13",
                                       taken_at=datetime(2023, 5, 1))
                # Even with camera metadata present, the name settles it.
                self.assertEqual(self.kind_of(path), "screenshot")

    def test_folder_name(self):
        path = synth.write_png(self.root / "Screenshots" / "IMG_0001.png",
                               make="Apple", model="iPhone 13",
                               taken_at=datetime(2023, 5, 1))
        meta = mediameta.extract(path)
        self.assertEqual(meta.kind, "screenshot")
        self.assertEqual(meta.screenshot_reason, "folder name")

    def test_exif_software_tag(self):
        path = self.root / "IMG_4242.jpg"
        block = synth.build_exif(datetime(2022, 3, 3), None, "Apple", "iPhone 13",
                                 software="Screenshot")
        path.write_bytes(synth.build_jpeg(block))
        self.assertEqual(self.kind_of(path), "screenshot")

    # --- caught by the absence of camera metadata --------------------------

    def test_bare_png_is_a_screenshot(self):
        path = synth.write_png(self.root / "IMG_0099.png")
        meta = mediameta.extract(path)
        self.assertEqual(meta.kind, "screenshot")
        self.assertEqual(meta.screenshot_reason, "PNG without camera metadata")

    def test_png_with_a_camera_is_a_photo(self):
        path = synth.write_png(self.root / "IMG_0100.png", make="Apple",
                               model="iPhone 13")
        self.assertEqual(self.kind_of(path), "photo")

    def test_png_with_gps_is_a_photo(self):
        path = synth.write_png(self.root / "IMG_0101.png", gps=BUCURESTI)
        self.assertEqual(self.kind_of(path), "photo")

    def test_png_with_an_exif_date_is_a_photo(self):
        path = synth.write_png(self.root / "IMG_0102.png",
                               taken_at=datetime(2024, 4, 4, 10, 0))
        self.assertEqual(self.kind_of(path), "photo")

    # --- the v0.1 regression this design exists to avoid -------------------

    def test_a_resized_photo_is_not_flagged(self):
        """v0.1 called anything at or below 1792x828 a screenshot.

        A photo shared through a messaging app comes back small and often
        stripped of some metadata; it is still a photo.
        """
        path = synth.write_png(self.root / "IMG_9001.png", make="Apple",
                               model="iPhone 13", width=800, height=600)
        self.assertEqual(self.kind_of(path), "photo")

        jpeg = synth.write_photo(self.root / "IMG_9002.jpg",
                                 datetime(2020, 2, 2, 8, 0), BUCURESTI)
        self.assertEqual(self.kind_of(jpeg), "photo")

    def test_jpeg_without_metadata_is_not_flagged(self):
        """Only PNGs trigger the metadata rule; a stripped JPEG stays a photo."""
        path = self.root / "IMG_9003.jpg"
        path.write_bytes(synth.build_jpeg(None))
        self.assertEqual(self.kind_of(path), "photo")

    def test_detection_can_be_switched_off(self):
        path = synth.write_png(self.root / "Screenshot 2023-05-01.png")
        self.assertEqual(
            mediameta.extract(path, detect_screenshots=False).kind, "photo")


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.taken = datetime(2025, 1, 14, 18, 30)

    def test_separate_root_is_the_default(self):
        cfg = Config(dest=Path("E:/Backup"))
        rel = organize.dest_relpath("Screenshot.png", self.taken, None,
                                    "screenshot", cfg)
        self.assertEqual(rel.as_posix(), "Screenshots/2025/01_ianuarie/Screenshot.png")

    def test_inside_the_month_folder(self):
        cfg = Config(dest=Path("E:/Backup"), screenshot_dir="./Screenshots")
        rel = organize.dest_relpath("Screenshot.png", self.taken, None,
                                    "screenshot", cfg)
        self.assertEqual(rel.as_posix(), "2025/01_ianuarie/Screenshots/Screenshot.png")

    def test_disabled_falls_back_to_the_place_tree(self):
        cfg = Config(dest=Path("E:/Backup"), screenshot_dir="")
        rel = organize.dest_relpath("Screenshot.png", self.taken, None,
                                    "screenshot", cfg)
        self.assertEqual(rel.as_posix(),
                         "2025/01_ianuarie/Fara_locatie/Screenshot.png")

    def test_screenshot_without_a_date(self):
        cfg = Config(dest=Path("E:/Backup"))
        rel = organize.dest_relpath("Screenshot.png", None, None, "screenshot", cfg)
        self.assertEqual(rel.as_posix(), "Screenshots/fara_data/Screenshot.png")

    def test_a_place_never_leaks_into_the_screenshot_tree(self):
        cfg = Config(dest=Path("E:/Backup"))
        rel = organize.dest_relpath("Screenshot.png", self.taken, "Barcelona",
                                    "screenshot", cfg)
        self.assertNotIn("Barcelona", rel.as_posix())


if __name__ == "__main__":
    unittest.main()
