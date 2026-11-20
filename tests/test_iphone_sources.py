"""Tests for the two iPhone backends, driven by stand-in devices.

These cover everything around the transport - walking the camera roll, reading
ranges, staging a file, and running the whole import pipeline against a source
that is not a folder. They cannot confirm that pymobiledevice3 or the Windows
shell behave as assumed; that still needs a phone.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ibackup import pipeline
from ibackup.config import Config
from ibackup.db import Database
from ibackup.geocode import Geocoder
from ibackup.reporter import Reporter
from ibackup.sources import afc as afc_module
from ibackup.sources import mtp as mtp_module
from tests import fakes, synth

BARCELONA = (41.3901, 2.1701)
CITIES = [("Barcelona", "ES", 41.39, 2.17, 1620000)]


class PhoneTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.phone = self.root / "phone"
        self.dest = self.root / "backup"
        self.report = Reporter(quiet=True)
        self.cfg = Config(dest=self.dest, infer_enabled=False)

    def tearDown(self):
        self.report.close()
        self._tmp.cleanup()

    def populate_phone(self):
        """A small camera roll: a Live Photo pair, a plain photo, a screenshot."""
        dcim = self.phone / "DCIM" / "100APPLE"
        dcim.mkdir(parents=True)
        synth.write_photo(dcim / "IMG_0001.JPG", datetime(2025, 1, 14, 18, 0),
                          BARCELONA, payload=b"one" * 300)
        (dcim / "IMG_0002.HEIC").write_bytes(
            synth.build_heic(synth.build_exif(datetime(2025, 1, 14, 18, 5),
                                              BARCELONA)))
        (dcim / "IMG_0002.MOV").write_bytes(
            synth.build_mov("2025-01-14T18:05:00+0100", "+41.3901+002.1701/"))
        second = self.phone / "DCIM" / "101APPLE"
        second.mkdir(parents=True)
        synth.write_png(second / "IMG_0100.PNG")
        (second / "readme.txt").write_text("not media", encoding="utf-8")
        return dcim

    def import_from(self, source):
        with Database(self.cfg.db_path) as db:
            geo = Geocoder(CITIES)
            importer = pipeline.Importer(self.cfg, db, geo, self.report)
            stats = importer.import_source(source)
            rows = [dict(r) for r in db.all_media()]
        return stats, rows

    def rel_paths(self):
        return sorted(p.relative_to(self.dest).as_posix()
                      for p in self.dest.rglob("*")
                      if p.is_file() and ".ibackup" not in p.parts)


# --- AFC --------------------------------------------------------------------

class AfcSourceTests(PhoneTestCase):
    def make_source(self, service=None, **kwargs):
        service = service or fakes.FakeAfcService(self.phone)
        with patch.object(afc_module, "_import_pymobiledevice3",
                          fakes.afc_import_hook(service)):
            source = afc_module.AfcSource(**kwargs)
        return source, service

    def test_walks_the_camera_roll(self):
        self.populate_phone()
        source, service = self.make_source()
        items = list(source.iter_items())

        names = sorted(i.name for i in items)
        self.assertEqual(names, ["IMG_0001.JPG", "IMG_0002.HEIC",
                                 "IMG_0002.MOV", "IMG_0100.PNG"])
        self.assertTrue(all(i.ref.startswith("/DCIM/") for i in items))
        self.assertTrue(all(i.kind == "afc" for i in items))
        self.assertTrue(all(i.size > 0 for i in items))
        # readme.txt is not media and never reaches the pipeline.
        self.assertNotIn("readme.txt", names)

    def test_the_device_name_reaches_the_label(self):
        self.populate_phone()
        source, _ = self.make_source()
        self.assertIn("Razvan's iPhone", source.label)

    def test_sizes_and_timestamps_are_carried_over(self):
        dcim = self.populate_phone()
        source, _ = self.make_source()
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        on_disk = dcim / "IMG_0001.JPG"
        self.assertEqual(item.size, on_disk.stat().st_size)
        self.assertAlmostEqual(item.mtime, on_disk.stat().st_mtime, places=0)

    def test_nanosecond_timestamps_are_converted(self):
        """Some AFC builds report mtime in nanoseconds."""
        self.populate_phone()
        source, service = self.make_source()
        real_stat = service.stat

        def nanosecond_stat(path):
            info = dict(real_stat(path))
            if info["st_ifmt"] == "S_IFREG":
                info["st_mtime"] = 1_700_000_000 * 1_000_000_000
            return info

        with patch.object(service, "stat", nanosecond_stat):
            item = next(iter(source.iter_items()))
        self.assertAlmostEqual(item.mtime, 1_700_000_000, places=0)

    def test_read_range_returns_the_right_bytes(self):
        dcim = self.populate_phone()
        source, service = self.make_source()
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        expected = (dcim / "IMG_0001.JPG").read_bytes()

        self.assertEqual(source.read_range(item, 0, 16), expected[:16])
        self.assertEqual(source.read_range(item, 10, 20), expected[10:30])
        self.assertGreater(service.calls["fseek"], 0)
        self.assertEqual(service.calls["get_file_contents"], 0)

    def test_a_device_without_seek_falls_back_to_whole_reads(self):
        dcim = self.populate_phone()
        service = fakes.FakeAfcServiceNoSeek(self.phone)
        source, _ = self.make_source(service)

        self.assertFalse(source.supports_random_read)
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        expected = (dcim / "IMG_0001.JPG").read_bytes()
        self.assertEqual(source.read_range(item, 4, 8), expected[4:12])

    def test_fetch_stages_the_file(self):
        dcim = self.populate_phone()
        source, _ = self.make_source()
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        staging = self.root / "staging"
        with source.localize(item, staging) as local:
            self.assertTrue(local.is_temp)
            self.assertEqual(local.path.read_bytes(),
                             (dcim / "IMG_0001.JPG").read_bytes())
            staged = local.path
        # Not claimed by the pipeline, so the staged copy is cleaned up.
        self.assertFalse(staged.exists())

    def test_no_device_raises_a_readable_error(self):
        with patch.object(afc_module, "_import_pymobiledevice3",
                          fakes.afc_import_hook(None)):
            with self.assertRaises(afc_module.AfcUnavailable) as ctx:
                afc_module.AfcSource()
        self.assertIn("Trust This Computer", str(ctx.exception))

    def test_missing_package_raises_a_readable_error(self):
        def explode():
            raise afc_module.AfcUnavailable(
                "pymobiledevice3 is not installed. Run: pip install pymobiledevice3")

        with patch.object(afc_module, "_import_pymobiledevice3", explode):
            with self.assertRaises(afc_module.AfcUnavailable) as ctx:
                afc_module.AfcSource()
        self.assertIn("pip install", str(ctx.exception))

    def test_an_unreadable_folder_does_not_stop_the_walk(self):
        self.populate_phone()
        source, service = self.make_source()
        real_listdir = service.listdir

        def flaky(path):
            if path.endswith("101APPLE"):
                raise OSError("device busy")
            return real_listdir(path)

        with patch.object(service, "listdir", flaky):
            names = sorted(i.name for i in source.iter_items())
        self.assertEqual(names, ["IMG_0001.JPG", "IMG_0002.HEIC", "IMG_0002.MOV"])


class AfcImportTests(PhoneTestCase):
    """The whole pipeline, against a source that is not a folder."""

    def make_source(self):
        service = fakes.FakeAfcService(self.phone)
        with patch.object(afc_module, "_import_pymobiledevice3",
                          fakes.afc_import_hook(service)):
            return afc_module.AfcSource(), service

    def test_import_from_the_phone(self):
        self.populate_phone()
        source, service = self.make_source()
        stats, rows = self.import_from(source)

        self.assertEqual(stats.imported, 4)
        self.assertEqual(stats.errors, 0)
        paths = self.rel_paths()
        for expected in ("2025/01_ianuarie/Barcelona/IMG_0001.JPG",
                         "2025/01_ianuarie/Barcelona/IMG_0002.HEIC",
                         "2025/01_ianuarie/Barcelona/IMG_0002.MOV"):
            self.assertIn(expected, paths)
        shot = [r for r in rows if r["kind"] == "screenshot"]
        self.assertEqual(len(shot), 1)
        self.assertTrue(shot[0]["rel_path"].startswith("Screenshots/"))
        # The Live Photo pair stayed together under one group.
        groups = {r["group_key"] for r in rows if r["group_key"]}
        self.assertEqual(len(groups), 1)
        self.assertTrue(all(r["source_kind"] == "afc" for r in rows))

    def test_a_photo_without_exif_keeps_the_phone_date(self):
        dcim = self.phone / "DCIM" / "100APPLE"
        photo = synth.write_photo(dcim / "IMG_0003.JPG", payload=b"three" * 300)
        stamp = datetime(2019, 7, 12, 12, 0).timestamp()
        os.utime(photo, (stamp, stamp))
        source, _ = self.make_source()
        self.import_from(source)

        self.assertEqual(self.rel_paths(),
                         ["2019/07_iulie/Fara_locatie/IMG_0003.JPG"])

    def test_a_backup_under_a_screenshot_folder_does_not_flag_phone_photos(self):
        self.dest = self.root / "Screenshot Backup" / "Photos"
        self.cfg = Config(dest=self.dest, infer_enabled=False)
        dcim = self.phone / "DCIM" / "100APPLE"
        synth.write_photo(dcim / "IMG_0001.JPG", datetime(2025, 1, 14, 18, 0),
                          BARCELONA, payload=b"one" * 300)
        source, _ = self.make_source()
        _, rows = self.import_from(source)

        self.assertEqual(rows[0]["kind"], "photo")
        self.assertEqual(self.rel_paths(),
                         ["2025/01_ianuarie/Barcelona/IMG_0001.JPG"])

    def test_staging_is_emptied_and_files_are_moved_not_copied(self):
        self.populate_phone()
        source, _ = self.make_source()
        self.import_from(source)
        self.assertEqual(list(self.cfg.staging_dir.iterdir()), [])
        self.assertEqual(list(self.dest.rglob("*.part")), [])

    def test_duplicates_are_found_without_downloading(self):
        """The point of AFC: 128 KB of reads decides it, not the whole file."""
        self.populate_phone()
        source, _ = self.make_source()
        self.import_from(source)

        cfg = Config(dest=self.dest, infer_enabled=False, use_source_cache=False)
        source, service = self.make_source()
        service.calls.clear()
        with Database(cfg.db_path) as db:
            importer = pipeline.Importer(cfg, db, Geocoder(CITIES), self.report)
            stats = importer.import_source(source)

        self.assertEqual(stats.duplicates, 4)
        self.assertEqual(stats.imported, 0)
        self.assertEqual(service.calls["get_file_contents"], 0)
        self.assertGreater(service.calls["fread"], 0)

    def test_a_file_vanishing_mid_run_is_reported_not_fatal(self):
        dcim = self.populate_phone()
        source, service = self.make_source()
        real_contents = service.get_file_contents

        def vanish(path):
            if path.endswith("IMG_0001.JPG"):
                raise OSError("file no longer available")
            return real_contents(path)

        with patch.object(service, "get_file_contents", vanish):
            stats, rows = self.import_from(source)

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.imported, 3)
        self.assertNotIn("IMG_0001.JPG", {r["filename"] for r in rows})


# --- MTP --------------------------------------------------------------------

class MtpSourceTests(PhoneTestCase):
    def make_source(self, copy_timeout=2.0, **kwargs):
        shell = fakes.FakeShell(self.phone, **kwargs)
        with patch.object(mtp_module, "_import_win32", fakes.mtp_import_hook(shell)):
            source = mtp_module.MtpSource(copy_timeout=copy_timeout)
        return source, shell

    def test_finds_the_phone_among_the_drives(self):
        self.populate_phone()
        source, _ = self.make_source()
        self.assertIn("Apple iPhone", source.label)

    def test_walks_the_camera_roll(self):
        self.populate_phone()
        source, _ = self.make_source()
        items = list(source.iter_items())
        names = sorted(i.name for i in items)
        self.assertEqual(names, ["IMG_0001.JPG", "IMG_0002.HEIC",
                                 "IMG_0002.MOV", "IMG_0100.PNG"])
        self.assertTrue(all(i.kind == "mtp" for i in items))
        self.assertTrue(all(i.size > 0 for i in items))

    def test_mtp_cannot_read_partially(self):
        """Which is why every file has to be fetched before it can be judged."""
        self.populate_phone()
        source, _ = self.make_source()
        self.assertFalse(source.supports_random_read)

    def test_copyhere_staging_round_trip(self):
        dcim = self.populate_phone()
        source, shell = self.make_source()
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        staging = self.root / "staging"
        with source.localize(item, staging) as local:
            self.assertEqual(local.path.read_bytes(),
                             (dcim / "IMG_0001.JPG").read_bytes())
        self.assertEqual(shell.copies, 1)
        # The per-item scratch folder is cleaned up behind us.
        leftovers = [p for p in staging.iterdir()] if staging.is_dir() else []
        self.assertEqual(leftovers, [])

    def test_a_copy_that_never_arrives_times_out(self):
        self.populate_phone()
        source, _ = self.make_source(copy_timeout=0.5, refuse_copies=True)
        item = next(i for i in source.iter_items() if i.name == "IMG_0001.JPG")
        with self.assertRaises(TimeoutError):
            with source.localize(item, self.root / "staging"):
                pass

    def test_no_device_raises_a_readable_error(self):
        shell = fakes.FakeShell(self.phone, device_name="Local Disk (C:)")
        with patch.object(mtp_module, "_import_win32", fakes.mtp_import_hook(shell)):
            with self.assertRaises(mtp_module.MtpUnavailable):
                mtp_module.MtpSource(device_name="Nokia 3310")

    def test_import_from_the_phone(self):
        self.populate_phone()
        source, _ = self.make_source()
        stats, rows = self.import_from(source)

        self.assertEqual(stats.imported, 4)
        self.assertEqual(stats.errors, 0)
        self.assertIn("2025/01_ianuarie/Barcelona/IMG_0001.JPG", self.rel_paths())
        self.assertTrue(all(r["source_kind"] == "mtp" for r in rows))
        self.assertEqual(list(self.cfg.staging_dir.iterdir()), [])

    def test_a_second_run_finds_duplicates(self):
        self.populate_phone()
        source, _ = self.make_source()
        self.import_from(source)
        source, _ = self.make_source()
        stats, rows = self.import_from(source)
        self.assertEqual(stats.imported, 0)
        self.assertEqual(stats.duplicates, 4)
        self.assertEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
