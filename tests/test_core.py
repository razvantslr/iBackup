"""Tests for the helpers, fingerprints, geocoding and path building."""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ibackup import geocode, hashing, organize, util
from ibackup.config import Config


class UtilTests(unittest.TestCase):
    def test_diacritics_are_folded(self):
        self.assertEqual(util.fold_ascii("Bucuresti"), "Bucuresti")
        self.assertEqual(util.fold_ascii("Bucure\u0219ti"), "Bucuresti")
        self.assertEqual(util.fold_ascii("T\u00e2rgu Mure\u0219"), "Targu Mures")
        self.assertEqual(util.fold_ascii("M\u00e1laga"), "Malaga")
        self.assertEqual(util.fold_ascii("K\u00f8benhavn"), "Kobenhavn")
        self.assertEqual(util.fold_ascii("Stra\u00dfe"), "Strasse")

    def test_sanitize_component(self):
        self.assertEqual(util.sanitize_component("Sfantu Gheorghe"), "Sfantu_Gheorghe")
        self.assertEqual(util.sanitize_component('bad:name?*'), "bad_name")
        self.assertEqual(util.sanitize_component("   "), "_")
        self.assertEqual(util.sanitize_component("."), "_")
        # Windows reserved names cannot be used as folders.
        self.assertTrue(util.sanitize_component("CON").startswith("_"))

    def test_sanitize_filename_keeps_extension(self):
        self.assertEqual(util.sanitize_filename("IMG 0001.HEIC"), "IMG_0001.HEIC")
        self.assertEqual(util.sanitize_filename("a:b.jpg"), "a_b.jpg")

    def test_month_folder(self):
        self.assertEqual(util.month_folder(1), "01_ianuarie")
        self.assertEqual(util.month_folder(12), "12_decembrie")
        self.assertEqual(util.month_folder(5, "en"), "05_may")

    def test_unique_path(self):
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "IMG_0001.jpg"
            self.assertEqual(util.unique_path(first), first)
            first.write_bytes(b"x")
            second = util.unique_path(first)
            self.assertEqual(second.name, "IMG_0001_2.jpg")
            # Paths reserved by the current run count even before they exist.
            taken = {str(second).lower()}
            third = util.unique_path(first, taken)
            self.assertEqual(third.name, "IMG_0001_3.jpg")


class HashingTests(unittest.TestCase):
    def test_partial_md5_is_stable_and_content_sensitive(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.bin"
            a.write_bytes(b"A" * 300_000)
            b = root / "b.bin"
            b.write_bytes(b"A" * 300_000)
            c = root / "c.bin"
            c.write_bytes(b"A" * 299_999 + b"B")

            self.assertEqual(hashing.partial_md5(a), hashing.partial_md5(b))
            self.assertNotEqual(hashing.partial_md5(a), hashing.partial_md5(c))

    def test_size_is_part_of_the_fingerprint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            short = root / "s.bin"
            short.write_bytes(b"AB")
            longer = root / "l.bin"
            longer.write_bytes(b"AB" * 2)
            self.assertNotEqual(hashing.partial_md5(short), hashing.partial_md5(longer))

    def test_reader_and_file_paths_agree(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            data = bytes(range(256)) * 2000
            path.write_bytes(data)

            def read_at(offset, length):
                return data[offset:offset + length]

            self.assertEqual(
                hashing.partial_md5(path),
                hashing.partial_md5_from_reader(read_at, len(data)),
            )

    def test_small_files_hash_whole_content(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.bin"
            path.write_bytes(b"0123456789")
            fp = hashing.fingerprint(path)
            self.assertEqual(fp.size, 10)
            self.assertEqual(fp.as_key(), (10, fp.partial_md5))


class GeocodeTests(unittest.TestCase):
    def setUp(self):
        self.cities = [
            ("Barcelona", "ES", 41.39, 2.17, 1620000),
            ("Badalona", "ES", 41.45, 2.25, 220000),
            ("Sitges", "ES", 41.24, 1.81, 28000),
            ("Bucuresti", "RO", 44.44, 26.10, 1830000),
        ]

    def test_prefers_the_big_city_within_radius(self):
        geo = geocode.Geocoder(self.cities, radius_km=25.0, prefer="population")
        # Coordinates in Badalona: we want "Barcelona", not a folder of its own.
        name, source = geo.place_name(41.45, 2.25)
        self.assertEqual(name, "Barcelona")
        self.assertEqual(source, "builtin")

    def test_nearest_mode(self):
        geo = geocode.Geocoder(self.cities, radius_km=25.0, prefer="nearest")
        name, _ = geo.place_name(41.45, 2.25)
        self.assertEqual(name, "Badalona")

    def test_small_radius_keeps_places_separate(self):
        geo = geocode.Geocoder(self.cities, radius_km=3.0)
        self.assertEqual(geo.place_name(41.24, 1.81)[0], "Sitges")

    def test_far_from_everything_gets_a_grid_label(self):
        geo = geocode.Geocoder(self.cities, radius_km=25.0)
        name, source = geo.place_name(10.0, 10.0)
        self.assertEqual(source, "grid")
        self.assertEqual(name, "GPS_10.0N_10.0E")

    def test_a_city_across_the_180th_meridian_is_found(self):
        geo = geocode.Geocoder([("Taveuni", "FJ", -16.8, -179.9, 10000)],
                               radius_km=25.0)
        # 0.15 degrees of longitude, about 16 km at this latitude.
        self.assertEqual(geo.place_name(-16.8, 179.95)[0], "Taveuni")

    def test_a_city_on_the_180th_meridian_is_found(self):
        geo = geocode.Geocoder([("Meridian", "FJ", -16.8, 180.0, 10000)],
                               radius_km=25.0)
        self.assertEqual(geo.place_name(-16.8, 179.95)[0], "Meridian")
        self.assertEqual(geo.place_name(-16.8, -179.95)[0], "Meridian")

    def test_grid_label_hemispheres(self):
        self.assertEqual(geocode.grid_label(-33.9, -70.7), "GPS_33.9S_70.7W")

    def test_aliases(self):
        geo = geocode.Geocoder(self.cities, aliases={"Bucuresti": "Acasa"})
        self.assertEqual(geo.place_name(44.44, 26.10)[0], "Acasa")

    def test_clustering_makes_nearby_photos_agree(self):
        geo = geocode.Geocoder(self.cities, radius_km=25.0)
        first = geo.cluster_key(41.3901234, 2.1701234)
        second = geo.cluster_key(41.3904321, 2.1704321)
        self.assertEqual(first, second)

    def test_haversine_known_distance(self):
        # Bucharest to Brasov, roughly 140 km as the crow flies.
        distance = geocode.haversine_km(44.44, 26.10, 45.65, 25.61)
        self.assertTrue(135 < distance < 145, distance)

    def test_no_place_without_coordinates(self):
        geo = geocode.Geocoder(self.cities)
        self.assertEqual(geo.place_name(None, None), ("", ""))

    def test_dataset_loading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cities.tsv"
            path.write_text("Cluj-Napoca\tRO\t46.77\t23.60\t324000\n"
                            "# a comment line\n"
                            "not a valid row\n", encoding="utf-8")
            geo = geocode.Geocoder.from_dataset(path, radius_km=25.0)
            self.assertEqual(len(geo.cities), 1)
            self.assertEqual(geo.place_name(46.77, 23.60)[0], "Cluj-Napoca")
            self.assertEqual(geo.source, "geonames")


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(dest=Path("E:/Backup"))

    def test_year_month_place(self):
        rel = organize.dest_relpath("IMG_0001.HEIC", datetime(2025, 1, 14, 18, 30),
                                    "Barcelona", "photo", self.cfg)
        self.assertEqual(rel.as_posix(), "2025/01_ianuarie/Barcelona/IMG_0001.HEIC")

    def test_missing_place_uses_unknown_folder(self):
        rel = organize.dest_relpath("IMG_0002.HEIC", datetime(2025, 3, 2),
                                    None, "photo", self.cfg)
        self.assertEqual(rel.as_posix(), "2025/03_martie/Fara_locatie/IMG_0002.HEIC")

    def test_missing_date_uses_dedicated_folder(self):
        rel = organize.dest_relpath("x.jpg", None, "Sibiu", "photo", self.cfg)
        self.assertEqual(rel.as_posix(), "fara_data/Sibiu/x.jpg")

    def test_video_subfolder_when_configured(self):
        cfg = Config(dest=Path("E:/Backup"), video_subdir="Video")
        rel = organize.dest_relpath("IMG_0003.MOV", datetime(2025, 7, 1),
                                    "Sinaia", "video", cfg)
        self.assertEqual(rel.as_posix(), "2025/07_iulie/Sinaia/Video/IMG_0003.MOV")

    def test_timestamp_renaming(self):
        cfg = Config(dest=Path("E:/Backup"), rename_with_timestamp=True)
        rel = organize.dest_relpath("IMG_0001.HEIC", datetime(2025, 1, 14, 18, 30, 42),
                                    "Barcelona", "photo", cfg)
        self.assertEqual(rel.name, "20250114_183042_IMG_0001.HEIC")

    def test_place_with_diacritics_and_spaces(self):
        rel = organize.dest_relpath("a.jpg", datetime(2024, 11, 5),
                                    "Sf\u00e2ntu Gheorghe", "photo", self.cfg)
        self.assertEqual(rel.as_posix(), "2024/11_noiembrie/Sfantu_Gheorghe/a.jpg")

    def test_english_month_names(self):
        cfg = Config(dest=Path("E:/Backup"), language="en")
        rel = organize.dest_relpath("a.jpg", datetime(2024, 2, 5), "Roma", "photo", cfg)
        self.assertEqual(rel.as_posix(), "2024/02_february/Roma/a.jpg")


if __name__ == "__main__":
    unittest.main()
