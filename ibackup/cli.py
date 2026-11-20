"""The command line: ibackup <command> [options]."""
from __future__ import annotations

import argparse
import platform
import sys
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, pipeline
from .config import MEDIA_KINDS, Config, load_config, write_default_config
from .db import Database, is_foreign_index
from .geocode import Geocoder, load_geonames
from .reporter import Reporter
from .sources import build_source, describe_backends
from .util import free_space, human_size

GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
DATASET_NAME = "cities.tsv"


# --- helpers ----------------------------------------------------------------

def _config_from_args(args) -> Config:
    cfg = load_config(dest=Path(args.dest) if args.dest else None,
                      config_file=Path(args.config) if args.config else None)
    if getattr(args, "strict_dedup", False):
        cfg.strict_remote_dedup = True
    if getattr(args, "no_infer", False):
        cfg.infer_enabled = False
    if getattr(args, "language", None):
        cfg.language = args.language
    if getattr(args, "radius_km", None):
        cfg.geo_radius_km = float(args.radius_km)
    if getattr(args, "no_cache", False):
        cfg.use_source_cache = False
    _apply_kind_filter(cfg, args)
    _refuse_foreign_index(cfg)
    return cfg


def _refuse_foreign_index(cfg: Config) -> None:
    """Every command with a --dest starts here, before anything is created."""
    if is_foreign_index(cfg.db_path):
        raise ValueError(
            f"{cfg.db_path} was made by another tool, not by this one, and was "
            f"left untouched (nothing was created or changed in {cfg.dest}). "
            f"Use a different --dest.")


def _apply_kind_filter(cfg: Config, args) -> None:
    """--only replaces the set of media kinds; --include and --exclude adjust it."""
    given = [(name, getattr(args, name, None))
             for name in ("only", "include", "exclude")
             if getattr(args, name, None)]
    if len(given) > 1:
        names = ", ".join(f"--{name}" for name, _ in given)
        raise ValueError(f"Use only one of --only, --include and --exclude "
                         f"(you passed {names}).")
    if not given:
        return
    name, values = given[0]
    values = set(values)
    if name == "only":
        kinds = values
    elif name == "include":
        kinds = cfg.allowed_kinds() | values
    else:
        kinds = cfg.allowed_kinds() - values
    if not kinds:
        raise ValueError("That filter excludes every media type, so there would "
                         "be nothing to copy.")
    cfg.kinds = sorted(kinds)


def _dataset_path(cfg: Config) -> Path | None:
    if cfg.geo_dataset:
        path = Path(cfg.geo_dataset)
        return path if path.is_file() else None
    builtin = cfg.internal_dir / DATASET_NAME
    return builtin if builtin.is_file() else None


def build_geocoder(cfg: Config, report: Reporter | None = None) -> Geocoder:
    kwargs = dict(radius_km=cfg.geo_radius_km, prefer=cfg.geo_prefer,
                  cluster_decimals=cfg.geo_cluster_decimals, aliases=cfg.geo_aliases)
    dataset = _dataset_path(cfg)
    if dataset is not None:
        try:
            geo = Geocoder.from_dataset(dataset, **kwargs)
            if report:
                report.info(f"Geocoding: {len(geo.cities)} places from {dataset.name}")
            return geo
        except Exception as exc:
            if report:
                report.warn(f"Could not read the dataset {dataset}: {exc}")
    geo = Geocoder(**kwargs)
    if report:
        report.info(f"Geocoding: built-in list ({len(geo.cities)} major cities). "
                    f"For full coverage: ibackup geonames --download")
    return geo


def _reporter(cfg: Config, args, name: str) -> Reporter:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if getattr(args, "dry_run", False):
        name += "-dryrun"
    log_path = cfg.log_dir / f"{name}-{stamp}.log"
    return Reporter(log_path=log_path, quiet=args.quiet, verbose=args.verbose)


def _add_kind_filters(parser) -> None:
    parser.add_argument("--only", nargs="+", choices=MEDIA_KINDS, metavar="KIND",
                        help=f"process only these media kinds ({', '.join(MEDIA_KINDS)})")
    parser.add_argument("--include", nargs="+", choices=MEDIA_KINDS, metavar="KIND",
                        help="add these kinds to the ones the config already takes")
    parser.add_argument("--exclude", nargs="+", choices=MEDIA_KINDS, metavar="KIND",
                        help="drop these kinds from the ones the config takes")


# --- commands ---------------------------------------------------------------

def cmd_init(args) -> int:
    cfg = _config_from_args(args)
    cfg.internal_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    path = write_default_config(cfg)
    with Database(cfg.db_path):
        pass
    print(f"Backup ready in {cfg.dest}")
    print(f"  config: {path}")
    print(f"  index : {cfg.db_path}")
    return 0


def cmd_import(args) -> int:
    cfg = _config_from_args(args)
    cfg.internal_dir.mkdir(parents=True, exist_ok=True)
    report = _reporter(cfg, args, "import")
    report.detail(f"ibackup {__version__}, Python {platform.python_version()}, "
                  f"{platform.platform()}")
    report.detail(
        f"Settings: language={cfg.language!r}, screenshot_dir={cfg.screenshot_dir!r}, "
        f"video_subdir={cfg.video_subdir!r}, "
        f"detect_screenshots={cfg.detect_screenshots}, "
        f"geo_radius_km={cfg.geo_radius_km}, geo_prefer={cfg.geo_prefer!r}, "
        f"infer_enabled={cfg.infer_enabled}, verify_after_copy={cfg.verify_after_copy}, "
        f"kinds={sorted(cfg.allowed_kinds())}, dry_run={args.dry_run}, "
        f"limit={args.limit}")
    try:
        with Database(cfg.db_path) as db:
            geo = build_geocoder(cfg, report)
            source = build_source(args.source, cfg)
            report.info(f"Free space on the backup drive: "
                        f"{human_size(free_space(cfg.dest))}")
            importer = pipeline.Importer(cfg, db, geo, report,
                                         dry_run=args.dry_run, limit=args.limit)
            try:
                stats = importer.import_source(source)
            finally:
                source.close()
            title = "Result (DRY-RUN, nothing copied):" if args.dry_run else "Result:"
            report.summary(title, stats.as_dict())
            return 1 if stats.errors else 0
    except KeyboardInterrupt:
        report.log_only("Interrupted by the user (Ctrl+C).")
        raise
    except Exception as exc:
        report.error(f"{type(exc).__name__}: {exc}")
        report.log_only(traceback.format_exc().rstrip())
        return 2
    finally:
        report.close()


def cmd_devices(args) -> int:
    print("Ways to reach the iPhone:\n")
    for info in describe_backends():
        mark = "YES" if info["available"] else "NO "
        print(f"  [{mark}] {info['backend'].upper():<4} {info['detail']}")
        for device in info["devices"]:
            print(f"          - {device}")
    print("\nAFC (pymobiledevice3) is the preferred backend: it sees the original")
    print("files and can check for duplicates without downloading the whole photo.")
    print("MTP works with the driver Windows ships, but it is slower.")
    return 0


def cmd_status(args) -> int:
    cfg = _config_from_args(args)
    with Database(cfg.db_path) as db:
        stats = db.stats()
    print(f"Backup: {cfg.dest}")
    print(f"  files        : {stats['n']} ({human_size(stats['bytes'] or 0)})")
    print(f"  photos/videos: {stats['photos'] or 0} / {stats['videos'] or 0}")
    print(f"  screenshots  : {stats['screenshots'] or 0}")
    print(f"  with GPS     : {stats['with_gps'] or 0}")
    print(f"  duplicates seen over time: {stats['duplicates_seen']}")
    if stats["first_taken"]:
        print(f"  range        : {stats['first_taken'][:10]} .. {stats['last_taken'][:10]}")
    if stats["years"]:
        print("\n  By year:")
        for row in stats["years"]:
            print(f"    {row['year']}  {row['n']}")
    if stats["places"]:
        print("\n  Top locations:")
        for row in stats["places"]:
            print(f"    {row['n']:>6}  {row['place']}")
    return 0


def cmd_verify(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "verify")
    try:
        with Database(cfg.db_path) as db:
            stats = pipeline.verify_backup(cfg, db, report, deep=args.deep)
        report.summary("Verification:", stats)
        bad = stats["missing"] + stats["size_mismatch"] + stats["hash_mismatch"]
        return 1 if bad else 0
    finally:
        report.close()


def cmd_relocate(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "relocate")
    try:
        with Database(cfg.db_path) as db:
            moved = pipeline.infer_locations(cfg, db, report, dry_run=args.dry_run)
        report.info(f"Locations guessed and files moved: {moved}")
        return 0
    finally:
        report.close()


def cmd_reorganize(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "reorganize")
    try:
        with Database(cfg.db_path) as db:
            stats = pipeline.reorganize(cfg, db, report, dry_run=args.dry_run)
        title = "Reorganize (DRY-RUN, nothing moved):" if args.dry_run else "Reorganize:"
        report.summary(title, stats)
        return 0
    finally:
        report.close()


def cmd_regeocode(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "regeocode")
    try:
        with Database(cfg.db_path) as db:
            geo = build_geocoder(cfg, report)
            stats = pipeline.regeocode(cfg, db, geo, report, dry_run=args.dry_run)
        title = "Regeocode (DRY-RUN, nothing moved):" if args.dry_run else "Regeocode:"
        report.summary(title, stats)
        return 0
    finally:
        report.close()


def cmd_reindex(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "reindex")
    try:
        with Database(cfg.db_path) as db:
            geo = build_geocoder(cfg, report)
            stats = pipeline.reindex(cfg, db, geo, report)
        report.summary("Reindex:", stats)
        return 0
    finally:
        report.close()


def cmd_geonames(args) -> int:
    cfg = _config_from_args(args)
    target = cfg.internal_dir / DATASET_NAME
    cfg.internal_dir.mkdir(parents=True, exist_ok=True)

    source_file = args.install
    temp_zip = None
    if args.download:
        import urllib.request
        temp_zip = cfg.internal_dir / "cities5000.zip"
        print(f"Downloading {GEONAMES_URL} (~2 MB)...")
        urllib.request.urlretrieve(GEONAMES_URL, temp_zip)
        source_file = str(temp_zip)
    if not source_file:
        print("Say what to install: --download or --install <file>", file=sys.stderr)
        return 2

    path = Path(source_file)
    created = [temp_zip] if temp_zip else []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                print("The archive contains no .txt file", file=sys.stderr)
                return 2
            extracted = cfg.internal_dir / names[0]
            with zf.open(names[0]) as src, extracted.open("wb") as dst:
                dst.write(src.read())
        path = extracted
        created.append(extracted)

    cities = load_geonames(path)
    if not cities:
        print(f"No cities found in {path}", file=sys.stderr)
        return 2
    with target.open("w", encoding="utf-8") as fh:
        for name, country, lat, lon, population in cities:
            fh.write(f"{name}\t{country}\t{lat}\t{lon}\t{population}\n")
    for leftover in created:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"Done: {len(cities)} places in {target}")
    return 0


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibackup",
        description="Back up photos from an iPhone or from old folders, sorted "
                    "by year / month / location, without duplicates.",
    )
    parser.add_argument("--dest", help="root of the backup on the SSD")
    parser.add_argument("--config", help="an explicit configuration file")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="prepare a new backup (config + index)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="copy from a source into the backup")
    p.add_argument("--source", required=True,
                   help="'iphone', 'iphone:afc', 'iphone:mtp' or a folder path")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen; copy nothing, only write a log")
    p.add_argument("--limit", type=int, help="stop after N files (for testing)")
    p.add_argument("--no-infer", action="store_true",
                   help="do not guess locations from photos close in time")
    p.add_argument("--strict-dedup", action="store_true",
                   help="confirm duplicates with a full MD5 for the iPhone too")
    p.add_argument("--language", choices=["ro", "en"], help="month folder language")
    p.add_argument("--radius-km", type=float, help="how far away a city may be")
    p.add_argument("--no-cache", action="store_true",
                   help="re-read every source file instead of trusting the "
                        "cached size+mtime fingerprint")
    _add_kind_filters(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("scan", help="same as 'import --dry-run'")
    p.add_argument("--source", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--language", choices=["ro", "en"])
    p.add_argument("--radius-km", type=float)
    _add_kind_filters(p)
    p.set_defaults(func=cmd_import, dry_run=True, no_infer=True, strict_dedup=False)

    p = sub.add_parser("devices", help="which iPhones are visible over USB")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("status", help="what the backup contains")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="check the backup against the index")
    p.add_argument("--deep", action="store_true", help="recompute the fingerprints")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("relocate", help="guess missing locations and move the files")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relocate)

    p = sub.add_parser("reorganize",
                       help="re-apply the layout rules to the whole backup")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reorganize)

    p = sub.add_parser("regeocode",
                       help="recompute the locations from the stored coordinates")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--radius-km", type=float, help="how far away a city may be")
    p.set_defaults(func=cmd_regeocode)

    p = sub.add_parser("reindex", help="rebuild the index from the existing files")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("geonames", help="install the place dataset for geocoding")
    p.add_argument("--download", action="store_true",
                   help=f"download {GEONAMES_URL}")
    p.add_argument("--install", help="use a cities5000.zip/.txt you already have")
    p.set_defaults(func=cmd_geonames)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in (("dry_run", False), ("limit", None), ("no_infer", False),
                          ("strict_dedup", False), ("deep", False),
                          ("language", None), ("radius_km", None),
                          ("only", None), ("include", None), ("exclude", None),
                          ("no_cache", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted. What was backed up so far is valid; rerun any time.",
              file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
