"""Linia de comanda: ibackup <comanda> [optiuni]."""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from . import pipeline
from .config import Config, load_config, write_default_config
from .db import Database
from .geocode import Geocoder, load_geonames
from .reporter import Reporter
from .sources import build_source, describe_backends
from .util import human_size

GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
DATASET_NAME = "cities.tsv"


# --- ajutoare ---------------------------------------------------------------

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
    return cfg


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
                report.info(f"Geocodare: {len(geo.cities)} localitati din {dataset.name}")
            return geo
        except Exception as exc:
            if report:
                report.warn(f"Nu am putut citi setul {dataset}: {exc}")
    geo = Geocoder(**kwargs)
    if report:
        report.info(f"Geocodare: lista interna ({len(geo.cities)} orase mari). "
                    f"Pentru acoperire completa: ibackup geonames --download")
    return geo


def _reporter(cfg: Config, args, name: str) -> Reporter:
    log_path = None
    if not getattr(args, "dry_run", False):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = cfg.log_dir / f"{name}-{stamp}.log"
    return Reporter(log_path=log_path, quiet=args.quiet, verbose=args.verbose)


# --- comenzi ----------------------------------------------------------------

def cmd_init(args) -> int:
    cfg = _config_from_args(args)
    cfg.internal_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    path = write_default_config(cfg)
    with Database(cfg.db_path):
        pass
    print(f"Backup pregatit in {cfg.dest}")
    print(f"  config: {path}")
    print(f"  index : {cfg.db_path}")
    return 0


def cmd_import(args) -> int:
    cfg = _config_from_args(args)
    cfg.internal_dir.mkdir(parents=True, exist_ok=True)
    report = _reporter(cfg, args, "import")
    try:
        with Database(cfg.db_path) as db:
            geo = build_geocoder(cfg, report)
            source = build_source(args.source, cfg)
            importer = pipeline.Importer(cfg, db, geo, report,
                                         dry_run=args.dry_run, limit=args.limit)
            try:
                stats = importer.import_source(source)
            finally:
                source.close()
            title = "Rezultat (DRY-RUN, nu s-a scris nimic):" if args.dry_run else "Rezultat:"
            report.summary(title, stats.as_dict())
            return 1 if stats.errors else 0
    except Exception as exc:
        report.error(f"{type(exc).__name__}: {exc}")
        return 2
    finally:
        report.close()


def cmd_devices(args) -> int:
    print("Metode de acces la iPhone:\n")
    for info in describe_backends():
        mark = "DA " if info["available"] else "NU "
        print(f"  [{mark}] {info['backend'].upper():<4} {info['detail']}")
        for device in info["devices"]:
            print(f"          - {device}")
    print("\nAFC (pymobiledevice3) e varianta preferata: vede fisierele originale")
    print("si poate verifica duplicatele fara sa descarce poza intreaga.")
    print("MTP merge cu driverul din Windows, dar e mai lent.")
    return 0


def cmd_status(args) -> int:
    cfg = _config_from_args(args)
    with Database(cfg.db_path) as db:
        stats = db.stats()
    print(f"Backup: {cfg.dest}")
    print(f"  fisiere      : {stats['n']} ({human_size(stats['bytes'] or 0)})")
    print(f"  poze / filme : {stats['photos'] or 0} / {stats['videos'] or 0}")
    print(f"  cu GPS       : {stats['with_gps'] or 0}")
    print(f"  duplicate vazute in timp: {stats['duplicates_seen']}")
    if stats["first_taken"]:
        print(f"  interval     : {stats['first_taken'][:10]} .. {stats['last_taken'][:10]}")
    if stats["years"]:
        print("\n  Pe ani:")
        for row in stats["years"]:
            print(f"    {row['year']}  {row['n']}")
    if stats["places"]:
        print("\n  Top locatii:")
        for row in stats["places"]:
            print(f"    {row['n']:>6}  {row['place']}")
    return 0


def cmd_verify(args) -> int:
    cfg = _config_from_args(args)
    report = _reporter(cfg, args, "verify")
    try:
        with Database(cfg.db_path) as db:
            stats = pipeline.verify_backup(cfg, db, report, deep=args.deep)
        report.summary("Verificare:", stats)
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
        report.info(f"Locatii deduse si mutate: {moved}")
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
        report.summary("Reindexare:", stats)
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
        print(f"Descarc {GEONAMES_URL} (~2 MB)...")
        urllib.request.urlretrieve(GEONAMES_URL, temp_zip)
        source_file = str(temp_zip)
    if not source_file:
        print("Spune ce instalez: --download sau --install <fisier>", file=sys.stderr)
        return 2

    path = Path(source_file)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                print("Arhiva nu contine niciun .txt", file=sys.stderr)
                return 2
            extracted = cfg.internal_dir / names[0]
            with zf.open(names[0]) as src, extracted.open("wb") as dst:
                dst.write(src.read())
        path = extracted

    cities = load_geonames(path)
    if not cities:
        print(f"Nu am gasit orase in {path}", file=sys.stderr)
        return 2
    with target.open("w", encoding="utf-8") as fh:
        for name, country, lat, lon, population in cities:
            fh.write(f"{name}\t{country}\t{lat}\t{lon}\t{population}\n")
    for leftover in (temp_zip, path if path != target else None):
        if leftover and Path(leftover).exists() and Path(leftover) != target:
            try:
                Path(leftover).unlink()
            except OSError:
                pass
    print(f"Gata: {len(cities)} localitati in {target}")
    return 0


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibackup",
        description="Backup de poze de pe iPhone sau din foldere vechi, "
                    "sortate pe an / luna / locatie, fara duplicate.",
    )
    parser.add_argument("--dest", help="radacina backup-ului de pe SSD")
    parser.add_argument("--config", help="fisier de configurare explicit")
    parser.add_argument("-q", "--quiet", action="store_true", help="fara progres")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="arata fiecare fisier")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="pregateste un backup nou (config + index)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="copiaza dintr-o sursa in backup")
    p.add_argument("--source", required=True,
                   help="'iphone', 'iphone:afc', 'iphone:mtp' sau calea unui folder")
    p.add_argument("--dry-run", action="store_true",
                   help="arata ce s-ar intampla, fara sa scrie nimic")
    p.add_argument("--limit", type=int, help="opreste-te dupa N fisiere (pentru teste)")
    p.add_argument("--no-infer", action="store_true",
                   help="nu deduce locatia din pozele vecine in timp")
    p.add_argument("--strict-dedup", action="store_true",
                   help="confirma duplicatele cu MD5 complet si pentru iPhone")
    p.add_argument("--language", choices=["ro", "en"], help="limba numelor de luni")
    p.add_argument("--radius-km", type=float, help="raza de cautare a orasului")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("scan", help="ca 'import --dry-run'")
    p.add_argument("--source", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--language", choices=["ro", "en"])
    p.add_argument("--radius-km", type=float)
    p.set_defaults(func=cmd_import, dry_run=True, no_infer=True, strict_dedup=False)

    p = sub.add_parser("devices", help="ce iPhone-uri se vad pe USB")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("status", help="ce contine backup-ul")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="verifica backup-ul fata de index")
    p.add_argument("--deep", action="store_true", help="recalculeaza si amprentele")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("relocate", help="deduce locatiile lipsa si muta fisierele")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relocate)

    p = sub.add_parser("reindex", help="reconstruieste indexul din fisierele existente")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("geonames", help="instaleaza setul de localitati pentru geocodare")
    p.add_argument("--download", action="store_true",
                   help=f"descarca {GEONAMES_URL}")
    p.add_argument("--install", help="foloseste un cities5000.zip/.txt deja descarcat")
    p.set_defaults(func=cmd_geonames)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in (("dry_run", False), ("limit", None), ("no_infer", False),
                          ("strict_dedup", False), ("deep", False),
                          ("language", None), ("radius_km", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nIntrerupt. Backup-ul de pana acum e valid, poti relua oricand.",
              file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Eroare: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
