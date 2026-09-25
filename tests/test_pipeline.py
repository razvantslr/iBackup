"""End-to-end pipeline tests: import, duplicates, groups, guessed locations."""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ibackup import cli, hashing, pipeline
from ibackup.config import Config
from ibackup.db import Database
from ibackup.geocode import Geocoder
from ibackup.reporter import Reporter
from ibackup.sources.folder import FolderSource
from tests import synth

BARCELONA = (41.3901, 2.1701)
BUCURESTI = (44.4268, 26.1025)

CITIES = [
    ("Barcelona", "ES", 41.39, 2.17, 1620000),
    ("Bucuresti", "RO", 44.44, 26.10, 1830000),
]


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "sursa"
        self.dest = self.root / "backup"
        self.src.mkdir()
        self.cfg = Config(dest=self.dest)
        self.report = Reporter(quiet=True)

    def tearDown(self):
        self.report.close()
        self._tmp.cleanup()

    # --- helpers ----------------------------------------------------------

    def photo(self, name, taken_at, gps=None, payload=None, subdir=""):
        target = self.src / subdir / name if subdir else self.src / name
        payload = payload if payload is not None else name.encode() * 40
        return synth.write_photo(target, taken_at, gps, payload=payload)

    def run_import(self, *, dry_run=False, cfg=None, source_dir=None):
        cfg = cfg or self.cfg
        with Database(cfg.db_path) as db:
            geo = Geocoder(CITIES, radius_km=cfg.geo_radius_km,
                           prefer=cfg.geo_prefer, aliases=cfg.geo_aliases)
            source = FolderSource(source_dir or self.src, skip_dirs=cfg.skip_dirs,
                                  accept_kinds=cfg.allowed_kinds())
            importer = pipeline.Importer(cfg, db, geo, self.report, dry_run=dry_run)
            stats = importer.import_source(source)
            rows = [dict(r) for r in db.all_media()]
        return stats, rows

    def rel_paths(self):
        return sorted(
            p.relative_to(self.dest).as_posix()
            for p in self.dest.rglob("*")
            if p.is_file() and ".ibackup" not in p.parts
        )


class ImportTests(PipelineTestCase):
    def test_sorts_by_year_month_and_place(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2024, 8, 3, 9, 0), BUCURESTI)
        stats, rows = self.run_import()

        self.assertEqual(stats.imported, 2)
        self.assertEqual(stats.duplicates, 0)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(self.rel_paths(), [
            "2024/08_august/Bucuresti/IMG_0002.jpg",
            "2025/01_ianuarie/Barcelona/IMG_0001.jpg",
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["place"], "Bucuresti")
        self.assertEqual(rows[0]["taken_at_source"], "exif:DateTimeOriginal")

    def test_source_files_are_never_touched(self):
        path = self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        before = path.read_bytes()
        self.run_import()
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), before)

    def test_second_run_finds_only_duplicates(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14, 18, 35), BARCELONA)
        self.run_import()
        stats, rows = self.run_import()

        self.assertEqual(stats.imported, 0)
        self.assertEqual(stats.duplicates, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.rel_paths()), 2)

    def test_same_photo_from_another_folder_is_a_duplicate(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        self.run_import()

        other = self.root / "backup_vechi"
        synth.write_photo(other / "vacanta" / "copie.jpg",
                          datetime(2025, 1, 14, 18, 30), BARCELONA,
                          payload=b"IMG_0001.jpg" * 40)
        stats, rows = self.run_import(source_dir=other)
        self.assertEqual(stats.imported, 0)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(len(rows), 1)

    def test_different_photos_with_the_same_name_both_survive(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA,
                   payload=b"primul" * 50, subdir="a")
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 31), BARCELONA,
                   payload=b"al doilea" * 50, subdir="b")
        stats, rows = self.run_import()

        self.assertEqual(stats.imported, 2)
        self.assertEqual(self.rel_paths(), [
            "2025/01_ianuarie/Barcelona/IMG_0001.jpg",
            "2025/01_ianuarie/Barcelona/IMG_0001_2.jpg",
        ])
        self.assertEqual(len({r["rel_path"] for r in rows}), 2)

    def test_photo_without_gps_goes_to_unknown_place(self):
        self.photo("IMG_0009.jpg", datetime(2021, 6, 1, 12, 0), None)
        stats, rows = self.run_import(cfg=Config(dest=self.dest, infer_enabled=False))
        self.assertEqual(stats.no_gps, 1)
        self.assertEqual(self.rel_paths(), ["2021/06_iunie/Fara_locatie/IMG_0009.jpg"])

    def test_dry_run_writes_nothing(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        stats, rows = self.run_import(dry_run=True)
        self.assertEqual(stats.imported, 1)
        self.assertEqual(self.rel_paths(), [])
        self.assertEqual(rows, [])

    def test_dry_run_detects_duplicates_within_the_same_run(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA,
                   payload=b"acelasi" * 50, subdir="a")
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14), BARCELONA,
                   payload=b"acelasi" * 50, subdir="b")
        stats, _ = self.run_import(dry_run=True)
        self.assertEqual(stats.imported, 1)
        self.assertEqual(stats.duplicates, 1)

    def test_non_media_files_are_ignored(self):
        (self.src / "note.txt").write_text("nothing to see here", encoding="utf-8")
        (self.src / "gol.jpg").write_bytes(b"")
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        stats, _ = self.run_import()
        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.imported, 1)


class LivePhotoTests(PipelineTestCase):
    def test_photo_and_movie_stay_together(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        (self.src / "IMG_0001.MOV").write_bytes(synth.build_mov(
            created="2025-01-14T18:30:00+0100", iso6709="+41.3901+002.1701/"))
        (self.src / "IMG_0001.AAE").write_text("<xml/>", encoding="utf-8")

        stats, rows = self.run_import()
        self.assertEqual(stats.imported, 3)
        self.assertEqual(stats.companions, 2)
        self.assertEqual(self.rel_paths(), [
            "2025/01_ianuarie/Barcelona/IMG_0001.AAE",
            "2025/01_ianuarie/Barcelona/IMG_0001.MOV",
            "2025/01_ianuarie/Barcelona/IMG_0001.jpg",
        ])
        self.assertEqual(len({r["group_key"] for r in rows}), 1)

    def test_companions_follow_a_renamed_primary(self):
        # A file with the same name is already at the destination, so the photo
        # becomes IMG_0001_2.jpg - its movie has to follow that name.
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA,
                   payload=b"vechi" * 50, subdir="a")
        self.run_import()

        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 31), BARCELONA,
                   payload=b"nou" * 50, subdir="b")
        (self.src / "b" / "IMG_0001.MOV").write_bytes(synth.build_mov(
            created="2025-01-14T18:31:00+0100", iso6709="+41.3901+002.1701/"))

        self.run_import()
        paths = self.rel_paths()
        self.assertIn("2025/01_ianuarie/Barcelona/IMG_0001_2.jpg", paths)
        self.assertIn("2025/01_ianuarie/Barcelona/IMG_0001_2.MOV", paths)

    def test_standalone_movie_is_treated_as_a_video(self):
        (self.src / "IMG_0007.MOV").write_bytes(synth.build_mov())
        stats, rows = self.run_import()
        self.assertEqual(stats.imported, 1)
        self.assertEqual(rows[0]["kind"], "video")
        self.assertEqual(rows[0]["rel_path"],
                         "2019/07_iulie/Bucuresti/IMG_0007.MOV")


class InferLocationTests(PipelineTestCase):
    def test_location_is_inferred_from_a_photo_taken_nearby_in_time(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14, 19, 0), None)
        stats, rows = self.run_import()

        self.assertEqual(stats.inferred_places, 1)
        self.assertEqual(self.rel_paths(), [
            "2025/01_ianuarie/Barcelona/IMG_0001.jpg",
            "2025/01_ianuarie/Barcelona/IMG_0002.jpg",
        ])
        inferred = [r for r in rows if r["filename"] == "IMG_0002.jpg"][0]
        self.assertEqual(inferred["place"], "Barcelona")
        self.assertEqual(inferred["place_source"], "inferred:time-neighbour")

    def test_photo_too_far_in_time_keeps_unknown_place(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 16, 12, 0), None)
        stats, _ = self.run_import()
        self.assertEqual(stats.inferred_places, 0)
        self.assertIn("2025/01_ianuarie/Fara_locatie/IMG_0002.jpg", self.rel_paths())

    def test_inference_can_be_disabled(self):
        cfg = Config(dest=self.dest, infer_enabled=False)
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14, 19, 0), None)
        stats, _ = self.run_import(cfg=cfg)
        self.assertEqual(stats.inferred_places, 0)
        self.assertIn("2025/01_ianuarie/Fara_locatie/IMG_0002.jpg", self.rel_paths())

    def test_empty_folders_are_cleaned_up_after_a_move(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14, 19, 0), None)
        self.run_import()
        self.assertFalse((self.dest / "2025/01_ianuarie/Fara_locatie").exists())


class ScreenshotTests(PipelineTestCase):
    def test_screenshots_are_filed_in_their_own_tree(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        synth.write_png(self.src / "Screenshot 2025-01-14 at 19.05.11.png")
        stats, rows = self.run_import()

        self.assertEqual(stats.imported, 2)
        self.assertEqual(self.rel_paths(), [
            "2025/01_ianuarie/Barcelona/IMG_0001.jpg",
            "Screenshots/2025/01_ianuarie/Screenshot_2025-01-14_at_19.05.11.png",
        ])
        kinds = {r["filename"]: r["kind"] for r in rows}
        self.assertEqual(kinds["IMG_0001.jpg"], "photo")

    def test_a_screenshot_does_not_get_a_guessed_location(self):
        """It has no GPS, but it must not be dragged into the photo's city."""
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        synth.write_png(self.src / "Screenshot 2025-01-14 at 18.30.00.png")
        stats, rows = self.run_import()

        self.assertEqual(stats.inferred_places, 0)
        shot = [r for r in rows if r["kind"] == "screenshot"][0]
        self.assertTrue(shot["rel_path"].startswith("Screenshots/"))

    def test_screenshot_dir_can_be_turned_off(self):
        cfg = Config(dest=self.dest, screenshot_dir="", infer_enabled=False)
        synth.write_png(self.src / "Screenshot 2025-01-14.png")
        self.run_import(cfg=cfg)
        self.assertEqual(self.rel_paths(),
                         ["2025/01_ianuarie/Fara_locatie/Screenshot_2025-01-14.png"])


class FreeSpaceTests(PipelineTestCase):
    def three_photos(self):
        names = ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]
        paths = [self.photo(n, datetime(2025, 1, 14, 18, i), BARCELONA)
                 for i, n in enumerate(names)]
        return [p.stat().st_size for p in paths]

    def test_import_stops_when_the_drive_fills_up(self):
        sizes = self.three_photos()
        cfg = Config(dest=self.dest, min_free_gb=0.0, infer_enabled=False)
        budget = sizes[0] + sizes[1]  # room for exactly two files

        with patch("ibackup.pipeline.free_space", side_effect=[budget, 0]):
            stats, rows = self.run_import(cfg=cfg)

        self.assertEqual(stats.imported, 2)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.rel_paths()), 2)

    def test_what_was_copied_before_stopping_stays_valid(self):
        sizes = self.three_photos()
        cfg = Config(dest=self.dest, min_free_gb=0.0, infer_enabled=False)
        with patch("ibackup.pipeline.free_space", side_effect=[sizes[0], 0]):
            self.run_import(cfg=cfg)

        with Database(cfg.db_path) as db:
            report = pipeline.verify_backup(cfg, db, self.report, deep=True)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["missing"], 0)
        self.assertEqual(report["hash_mismatch"], 0)
        self.assertEqual(list(self.dest.rglob("*.part")), [])

    def test_the_reserve_is_respected(self):
        """Plenty of raw space, but min_free_gb must stay untouched."""
        self.three_photos()
        cfg = Config(dest=self.dest, min_free_gb=1.0, infer_enabled=False)
        with patch("ibackup.pipeline.free_space", return_value=500 * 1024 * 1024):
            stats, rows = self.run_import(cfg=cfg)

        self.assertEqual(stats.imported, 0)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(self.rel_paths(), [])

    def test_a_normal_run_is_not_disturbed(self):
        self.three_photos()
        stats, _ = self.run_import()
        self.assertEqual(stats.imported, 3)
        self.assertEqual(stats.errors, 0)


class KindFilterTests(PipelineTestCase):
    def mixed_source(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        synth.write_png(self.src / "Screenshot 2025-01-14 at 19.00.00.png")
        (self.src / "IMG_0050.MOV").write_bytes(synth.build_mov())

    def test_only_keeps_a_single_kind(self):
        self.mixed_source()
        cfg = Config(dest=self.dest, kinds=["video"], infer_enabled=False)
        stats, rows = self.run_import(cfg=cfg)

        self.assertEqual(stats.imported, 1)
        self.assertEqual(stats.scanned, 1)  # the rest never got enumerated
        self.assertEqual(rows[0]["kind"], "video")

    def test_excluding_screenshots_still_scans_the_photos(self):
        self.mixed_source()
        cfg = Config(dest=self.dest,
                     kinds=["photo", "video", "sidecar"], infer_enabled=False)
        stats, rows = self.run_import(cfg=cfg)

        # A screenshot only reveals itself once its metadata is read, so it is
        # scanned and then dropped, not skipped during enumeration.
        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.filtered, 1)
        self.assertEqual(stats.imported, 2)
        self.assertNotIn("screenshot", {r["kind"] for r in rows})
        self.assertEqual(list(self.dest.glob("Screenshots")), [])

    def test_excluding_videos_skips_them_during_enumeration(self):
        self.mixed_source()
        cfg = Config(dest=self.dest,
                     kinds=["photo", "screenshot", "sidecar"], infer_enabled=False)
        stats, _ = self.run_import(cfg=cfg)
        self.assertEqual(stats.scanned, 2)
        self.assertEqual(stats.filtered, 0)


class KindFilterArgumentTests(unittest.TestCase):
    """The --only / --include / --exclude resolution, without touching disk."""

    def resolve(self, **kwargs):
        cfg = Config(dest=Path("E:/Backup"))
        fields = {"only": None, "include": None, "exclude": None}
        fields.update(kwargs)
        cli._apply_kind_filter(cfg, SimpleNamespace(**fields))
        return cfg.allowed_kinds()

    def test_no_filter_uses_the_config_defaults(self):
        self.assertEqual(self.resolve(),
                         {"photo", "screenshot", "video", "sidecar"})

    def test_only_replaces_the_set(self):
        self.assertEqual(self.resolve(only=["video"]), {"video"})

    def test_exclude_removes_from_the_defaults(self):
        self.assertEqual(self.resolve(exclude=["screenshot", "sidecar"]),
                         {"photo", "video"})

    def test_include_adds_to_the_defaults(self):
        cfg = Config(dest=Path("E:/Backup"), include_videos=False)
        args = SimpleNamespace(only=None, include=["video"], exclude=None)
        cli._apply_kind_filter(cfg, args)
        self.assertIn("video", cfg.allowed_kinds())

    def test_two_filters_at_once_are_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.resolve(only=["photo"], exclude=["video"])
        self.assertIn("only one of", str(ctx.exception))

    def test_a_filter_that_leaves_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            self.resolve(exclude=["photo", "screenshot", "video", "sidecar"])


class FingerprintCacheTests(PipelineTestCase):
    def test_rerunning_the_same_folder_reads_nothing(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2025, 1, 14, 18, 5), BARCELONA)
        self.run_import()

        with patch("ibackup.pipeline.partial_md5_from_reader",
                   wraps=hashing.partial_md5_from_reader) as partial,              patch("ibackup.pipeline.file_md5", wraps=hashing.file_md5) as full:
            stats, rows = self.run_import()

        self.assertEqual(stats.duplicates, 2)
        self.assertEqual(stats.imported, 0)
        self.assertEqual(partial.call_count, 0)
        self.assertEqual(full.call_count, 0)
        self.assertEqual(len(rows), 2)

    def test_a_changed_source_file_is_re_read(self):
        path = self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.run_import()

        synth.write_photo(path, datetime(2025, 1, 14, 18, 0), BARCELONA,
                          payload=b"different content entirely" * 30)
        os.utime(path, (time.time(), time.time()))

        stats, rows = self.run_import()
        self.assertEqual(stats.imported, 1)
        self.assertEqual(len(rows), 2)

    def test_no_cache_forces_a_re_read(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.run_import()

        cfg = Config(dest=self.dest, use_source_cache=False, infer_enabled=False)
        with patch("ibackup.pipeline.partial_md5_from_reader",
                   wraps=hashing.partial_md5_from_reader) as partial:
            stats, _ = self.run_import(cfg=cfg)
        self.assertEqual(stats.duplicates, 1)
        self.assertGreater(partial.call_count, 0)

    def test_a_deleted_backup_file_is_copied_again(self):
        """The cache records a decision, not a promise the file still exists."""
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 0), BARCELONA)
        self.run_import()
        with Database(self.cfg.db_path) as db:
            db.conn.execute("DELETE FROM media")
            db.conn.commit()

        stats, rows = self.run_import()
        self.assertEqual(stats.imported, 1)
        self.assertEqual(len(rows), 1)


class PartialCollisionTests(PipelineTestCase):
    """Two different files can share a partial fingerprint; only content decides."""

    HEAD = b"H" * 65536
    TAIL = b"T" * 65536

    def write_pair(self):
        first = self.src / "IMG_20240101_100000.jpg"
        second = self.src / "IMG_20240101_100001.jpg"
        first.write_bytes(self.HEAD + b"A" * 70000 + self.TAIL)
        second.write_bytes(self.HEAD + b"B" * 70000 + self.TAIL)
        return first, second

    def test_the_two_files_really_do_collide(self):
        first, second = self.write_pair()
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        self.assertEqual(hashing.partial_md5(first), hashing.partial_md5(second))
        self.assertNotEqual(hashing.file_md5(first), hashing.file_md5(second))

    def test_both_files_are_kept(self):
        self.write_pair()
        cfg = Config(dest=self.dest, infer_enabled=False)
        stats, rows = self.run_import(cfg=cfg)

        self.assertEqual(stats.imported, 2)
        self.assertEqual(stats.duplicates, 0)
        self.assertEqual(len(rows), 2)

    def test_without_confirmation_the_collision_would_lose_a_file(self):
        """Documents what confirm_with_full_md5 = false gives up."""
        self.write_pair()
        cfg = Config(dest=self.dest, confirm_with_full_md5=False,
                     infer_enabled=False)
        stats, _ = self.run_import(cfg=cfg)
        self.assertEqual(stats.imported, 1)
        self.assertEqual(stats.duplicates, 1)


class VerifyAndReindexTests(PipelineTestCase):
    def test_verify_passes_then_catches_damage(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        self.run_import()

        with Database(self.cfg.db_path) as db:
            clean = pipeline.verify_backup(self.cfg, db, self.report, deep=True)
        self.assertEqual(clean["missing"], 0)
        self.assertEqual(clean["hash_mismatch"], 0)
        self.assertEqual(clean["checked"], 1)

        target = self.dest / "2025/01_ianuarie/Barcelona/IMG_0001.jpg"
        data = bytearray(target.read_bytes())
        data[-1] ^= 0xFF
        target.write_bytes(bytes(data))
        with Database(self.cfg.db_path) as db:
            damaged = pipeline.verify_backup(self.cfg, db, self.report, deep=True)
        self.assertEqual(damaged["hash_mismatch"], 1)

    def test_verify_reports_missing_files(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        self.run_import()
        (self.dest / "2025/01_ianuarie/Barcelona/IMG_0001.jpg").unlink()
        with Database(self.cfg.db_path) as db:
            stats = pipeline.verify_backup(self.cfg, db, self.report)
        self.assertEqual(stats["missing"], 1)

    def test_reindex_rebuilds_the_index(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14, 18, 30), BARCELONA)
        self.photo("IMG_0002.jpg", datetime(2024, 8, 3, 9, 0), BUCURESTI)
        self.run_import()

        self.cfg.db_path.unlink()
        for leftover in self.cfg.internal_dir.glob("index.db-*"):
            leftover.unlink()

        with Database(self.cfg.db_path) as db:
            geo = Geocoder(CITIES)
            stats = pipeline.reindex(self.cfg, db, geo, self.report)
            rows = [dict(r) for r in db.all_media()]
        self.assertEqual(stats["added"], 2)
        self.assertEqual({r["place"] for r in rows}, {"Barcelona", "Bucuresti"})

        # After a reindex, importing the same photos copies nothing.
        stats, _ = self.run_import()
        self.assertEqual(stats.imported, 0)
        self.assertEqual(stats.duplicates, 2)


class SourceSelectionTests(PipelineTestCase):
    def test_refuses_source_equal_to_destination(self):
        from ibackup.sources import build_source
        with self.assertRaises(ValueError) as ctx:
            build_source(str(self.dest), self.cfg)
        self.assertIn("reindex", str(ctx.exception))

    def test_refuses_source_inside_the_backup(self):
        from ibackup.sources import build_source
        inner = self.dest / "2025"
        inner.mkdir(parents=True)
        with self.assertRaises(ValueError):
            build_source(str(inner), self.cfg)

    def test_refuses_backup_inside_the_source(self):
        from ibackup.sources import build_source
        cfg = Config(dest=self.src / "backup")
        with self.assertRaises(ValueError):
            build_source(str(self.src), cfg)

    def test_accepts_a_sibling_folder(self):
        from ibackup.sources import build_source
        source = build_source(str(self.src), self.cfg)
        self.assertEqual(source.kind, "folder")
        source.close()


class RobustnessTests(PipelineTestCase):
    def test_corrupt_file_is_imported_using_the_filename_date(self):
        (self.src / "IMG_20230415_120000.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 200)
        stats, rows = self.run_import()
        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.imported, 1)
        self.assertEqual(rows[0]["rel_path"],
                         "2023/04_aprilie/Fara_locatie/IMG_20230415_120000.jpg")

    def test_no_leftover_part_files(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        self.run_import()
        self.assertEqual(list(self.dest.rglob("*.part")), [])

    def test_staging_is_emptied(self):
        self.photo("IMG_0001.jpg", datetime(2025, 1, 14), BARCELONA)
        self.run_import()
        staging = self.cfg.staging_dir
        if staging.exists():
            self.assertEqual(list(staging.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
