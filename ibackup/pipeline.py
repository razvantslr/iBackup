"""Motorul propriu-zis: scaneaza sursa, elimina duplicatele, asaza fisierele.

Traseul unui fisier:
  1. amprenta rapida (dimensiune + MD5 partial)
  2. cautare in index -> daca exista deja, e duplicat si il sarim
  3. citirea metadatelor (data + GPS)
  4. GPS -> nume de oras (geocodare offline)
  5. copiere atomica in <an>/<luna>/<locatie>/ si inregistrare in index

Nimic nu se sterge vreodata din sursa. Scrierea la destinatie se face prin
fisier temporar + rename, deci o intrerupere nu lasa fisiere pe jumatate.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import organize
from .config import Config
from .db import Database
from .geocode import Geocoder
from .hashing import Fingerprint, file_md5, partial_md5, partial_md5_from_reader
from .mediameta import MediaMeta, extract, kind_for_name, utc_now
from .reporter import Reporter
from .sources.base import Localized, Source, SourceItem
from .util import unique_path

_PRIMARY_ORDER = {"photo": 0, "video": 1, "sidecar": 2, "other": 3}


@dataclass
class Anchor:
    """Ce a decis fisierul principal dintr-un grup (poza unui Live Photo)."""
    dest_dir: PurePosixPath
    stem: str
    taken_at: datetime | None
    place: str
    place_source: str
    lat: float | None
    lon: float | None
    gps_source: str


@dataclass
class Stats:
    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    companions: int = 0
    errors: int = 0
    no_exif_date: int = 0
    no_gps: int = 0
    bytes_copied: int = 0
    inferred_places: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def pick_primary(group: list[SourceItem]) -> SourceItem:
    """Intr-un Live Photo, poza comanda; filmul si .AAE o urmeaza."""
    return min(group, key=lambda it: (_PRIMARY_ORDER.get(kind_for_name(it.name), 9),
                                      it.name.lower()))


def group_items(items):
    """Grupeaza fisierele cu acelasi nume de baza din acelasi folder."""
    buffer: dict[tuple[str, str], list[SourceItem]] = {}
    current_dir = None
    for item in items:
        if current_dir is not None and item.rel_dir != current_dir and buffer:
            yield from _flush(buffer)
            buffer = {}
        current_dir = item.rel_dir
        buffer.setdefault((item.rel_dir, item.stem.lower()), []).append(item)
    if buffer:
        yield from _flush(buffer)


def _flush(buffer):
    for key in sorted(buffer):
        yield buffer[key]


class Importer:
    def __init__(self, cfg: Config, db: Database, geocoder: Geocoder,
                 reporter: Reporter, *, dry_run: bool = False,
                 limit: int | None = None):
        self.cfg = cfg
        self.db = db
        self.geo = geocoder
        self.report = reporter
        self.dry_run = dry_run
        self.limit = limit
        self.stats = Stats()
        self.run_id: int | None = None
        self._taken: set[str] = set()          # cai rezervate in rularea curenta
        self._dry_seen: dict[tuple[int, str], str] = {}  # duplicate in dry-run
        self.unknown_place = organize.place_folder(None, cfg)

    # --- API ---------------------------------------------------------------

    def import_source(self, source: Source) -> Stats:
        mode = "dry-run" if self.dry_run else "import"
        if not self.dry_run:
            self.run_id = self.db.start_run(source.label, str(self.cfg.dest), mode)
            self.cfg.staging_dir.mkdir(parents=True, exist_ok=True)

        self.report.info(f"Sursa: {source.label}")
        self.report.info(f"Destinatie: {self.cfg.dest}"
                         + ("  [DRY-RUN, nu se scrie nimic]" if self.dry_run else ""))

        try:
            for group in group_items(source.iter_items()):
                if self.limit is not None and self.stats.scanned >= self.limit:
                    self.report.info(f"M-am oprit la limita de {self.limit} fisiere.")
                    break
                self._process_group(source, group)
                self.report.progress(self.stats.scanned, source.count_hint(),
                                     f"{self.stats.imported} noi, "
                                     f"{self.stats.duplicates} duplicate")
        finally:
            if not self.dry_run:
                self.db.conn.commit()
                self._cleanup_staging()

        if self.cfg.infer_enabled and not self.dry_run:
            self.stats.inferred_places = infer_locations(
                self.cfg, self.db, self.report, dry_run=False
            )

        if self.run_id is not None:
            self.db.finish_run(self.run_id, self.stats.as_dict())
        return self.stats

    # --- grup --------------------------------------------------------------

    def _process_group(self, source: Source, group: list[SourceItem]) -> None:
        primary = pick_primary(group)
        others = [it for it in group if it is not primary]
        group_key = None
        if others:
            group_key = f"{primary.rel_dir}|{primary.stem}".lower()
        anchor = self._safe_process(source, primary, group_key, None)
        for item in others:
            self.stats.companions += 1
            self._safe_process(source, item, group_key, anchor)

    def _safe_process(self, source, item, group_key, anchor) -> Anchor | None:
        try:
            return self._process_item(source, item, group_key, anchor)
        except Exception as exc:
            self.stats.errors += 1
            message = f"{item.ref}: {type(exc).__name__}: {exc}"
            self.report.error(message)
            if not self.dry_run and self.run_id is not None:
                try:
                    self.db.log_error(self.run_id, item.ref, message)
                except Exception:
                    pass
            return None

    # --- fisier ------------------------------------------------------------

    def _process_item(self, source: Source, item: SourceItem,
                      group_key: str | None, anchor: Anchor | None) -> Anchor | None:
        self.stats.scanned += 1
        chunk = self.cfg.partial_chunk

        if source.supports_random_read:
            fp = Fingerprint(
                size=item.size,
                partial_md5=partial_md5_from_reader(
                    lambda off, length: source.read_range(item, off, length),
                    item.size, chunk,
                ),
            )
            matched = self._find_duplicate(item, fp, local_path=None, source=source)
            if matched is not None:
                return self._on_duplicate(item, fp, matched)
            with source.localize(item, self.cfg.staging_dir) as local:
                return self._ingest(item, local, fp, group_key, anchor)

        with source.localize(item, self.cfg.staging_dir) as local:
            size = local.path.stat().st_size
            fp = Fingerprint(size=size, partial_md5=partial_md5(local.path, size, chunk))
            matched = self._find_duplicate(item, fp, local_path=local.path, source=source)
            if matched is not None:
                return self._on_duplicate(item, fp, matched)
            return self._ingest(item, local, fp, group_key, anchor)

    def _find_duplicate(self, item: SourceItem, fp: Fingerprint,
                        local_path: Path | None, source: Source):
        """Intoarce randul din index care are acelasi continut, sau None."""
        if self.dry_run:
            seen = self._dry_seen.get(fp.as_key())
            if seen is not None:
                return {"id": -1, "rel_path": seen, "place": None, "lat": None,
                        "lon": None, "gps_source": "", "taken_at": None,
                        "place_source": ""}
            rows = self.db.find_by_fingerprint(fp.size, fp.partial_md5)
            return rows[0] if rows else None

        rows = self.db.find_by_fingerprint(fp.size, fp.partial_md5)
        if not rows:
            return None
        if not self.cfg.confirm_with_full_md5:
            return rows[0]

        # Confirmarea cere continutul integral. Daca sursa e telefonul, ar
        # insemna sa descarcam fisierul tocmai ca sa aflam ca il avem deja;
        # o facem doar daca utilizatorul a cerut explicit --strict-dedup.
        if local_path is None and not self.cfg.strict_remote_dedup:
            return rows[0]
        if local_path is None:
            with source.localize(item, self.cfg.staging_dir) as local:
                candidate_md5 = file_md5(local.path)
        else:
            candidate_md5 = file_md5(local_path)

        for row in rows:
            known = row["full_md5"]
            if known is None:
                existing = self.cfg.dest / row["rel_path"]
                if not existing.is_file():
                    continue
                known = file_md5(existing)
                self.db.set_full_md5(row["id"], known)
            if known == candidate_md5:
                return row
        return None  # aceeasi amprenta partiala, continut diferit: nu e duplicat

    def _on_duplicate(self, item: SourceItem, fp: Fingerprint, row) -> Anchor | None:
        self.stats.duplicates += 1
        self.report.detail(f"duplicat: {item.name} -> {row['rel_path']}")
        if not self.dry_run:
            self.db.record_duplicate(self.run_id, item, int(row["id"]),
                                     fp.size, fp.partial_md5)
        return self._anchor_from_row(row)

    def _anchor_from_row(self, row) -> Anchor | None:
        rel = row["rel_path"] if not isinstance(row, dict) else row.get("rel_path")
        if not rel:
            return None
        path = PurePosixPath(rel)
        taken = None
        raw = row["taken_at"] if not isinstance(row, dict) else row.get("taken_at")
        if raw:
            try:
                taken = datetime.fromisoformat(raw)
            except ValueError:
                taken = None
        get = row.get if isinstance(row, dict) else row.__getitem__
        return Anchor(
            dest_dir=path.parent, stem=path.stem, taken_at=taken,
            place=get("place") or self.unknown_place,
            place_source=get("place_source") or "",
            lat=get("lat"), lon=get("lon"), gps_source=get("gps_source") or "",
        )

    # --- scrierea la destinatie --------------------------------------------

    def _ingest(self, item: SourceItem, local: Localized, fp: Fingerprint,
                group_key: str | None, anchor: Anchor | None) -> Anchor:
        kind = kind_for_name(item.name)
        if kind == "sidecar":
            meta = MediaMeta(kind="sidecar")
        else:
            meta = extract(local.path)

        if meta.taken_at is None and item.mtime:
            meta.taken_at = datetime.fromtimestamp(item.mtime)
            meta.taken_at_source = "source-mtime"

        if anchor is not None:
            # Fisierele pereche merg langa poza lor, cu aceleasi date.
            taken_at = anchor.taken_at or meta.taken_at
            place, place_source = anchor.place, anchor.place_source
            lat, lon = (meta.lat, meta.lon) if meta.has_gps else (anchor.lat, anchor.lon)
            gps_source = meta.gps_source or anchor.gps_source
            rel_dir = anchor.dest_dir
            stem_override = anchor.stem
        else:
            taken_at = meta.taken_at
            lat, lon, gps_source = meta.lat, meta.lon, meta.gps_source
            place, place_source = self._resolve_place(lat, lon)
            rel_dir = organize.dest_dir(taken_at, place, meta.kind, self.cfg)
            stem_override = None

        # Fisierele pereche (.AAE) n-au metadate proprii, deci nu le numaram
        # ca lipsuri - ar umfla degeaba statisticile.
        if meta.kind in ("photo", "video"):
            if not meta.taken_at_source.startswith(("exif", "qt")):
                self.stats.no_exif_date += 1
            if lat is None:
                self.stats.no_gps += 1

        filename = organize.target_filename(item.name, taken_at, self.cfg, stem_override)
        dest_abs = unique_path(self.cfg.dest / rel_dir / filename, self._taken)
        rel_final = dest_abs.relative_to(self.cfg.dest).as_posix()
        self._taken.add(str(dest_abs).lower())

        if self.dry_run:
            self.stats.imported += 1
            self.stats.bytes_copied += fp.size
            self._dry_seen[fp.as_key()] = rel_final
            self.report.detail(f"ar copia: {item.name} -> {rel_final}")
        else:
            self._place_file(local, dest_abs, fp, item)
            self.stats.imported += 1
            self.stats.bytes_copied += fp.size
            self.report.detail(f"copiat: {item.name} -> {rel_final}")
            self.db.insert_media(
                size=fp.size, partial_md5=fp.partial_md5, full_md5=None,
                rel_path=rel_final, filename=dest_abs.name, kind=meta.kind,
                taken_at=taken_at.isoformat() if taken_at else None,
                taken_at_source=meta.taken_at_source, lat=lat, lon=lon,
                gps_source=gps_source, place=place, place_source=place_source,
                camera_make=meta.make, camera_model=meta.model,
                group_key=group_key, companion_of=None,
                source_kind=item.kind, source_ref=item.ref,
                source_name=item.name, imported_at=utc_now(), run_id=self.run_id,
            )

        return Anchor(
            dest_dir=PurePosixPath(rel_final).parent,
            stem=PurePosixPath(rel_final).stem, taken_at=taken_at, place=place,
            place_source=place_source, lat=lat, lon=lon, gps_source=gps_source,
        )

    def _place_file(self, local: Localized, dest_abs: Path, fp: Fingerprint,
                    item: SourceItem) -> None:
        """Scriere atomica: intai .part, apoi rename. Verificam ce a ajuns pe disc."""
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        temp = dest_abs.with_name(dest_abs.name + ".part")
        if temp.exists():
            temp.unlink()
        if local.is_temp:
            os.replace(local.path, temp)   # staging e pe acelasi volum: instant
            local.claimed = True
        else:
            shutil.copy2(local.path, temp)

        if self.cfg.verify_after_copy:
            written = temp.stat().st_size
            check = partial_md5(temp, written, self.cfg.partial_chunk)
            if written != fp.size or check != fp.partial_md5:
                temp.unlink(missing_ok=True)
                raise IOError(
                    f"verificarea a esuat dupa copiere ({item.name}): "
                    f"{written} octeti vs {fp.size} asteptati"
                )
        if item.mtime:
            try:
                os.utime(temp, (item.mtime, item.mtime))
            except OSError:
                pass
        os.replace(temp, dest_abs)

    def _resolve_place(self, lat, lon) -> tuple[str, str]:
        if lat is None or lon is None:
            return self.unknown_place, ""
        key = self.geo.cluster_key(lat, lon)
        if not self.dry_run:
            cached = self.db.cached_place(key[0], key[1])
            if cached:
                return cached
        name, source = self.geo.place_name(lat, lon)
        folder = organize.place_folder(name, self.cfg)
        if not self.dry_run:
            self.db.cache_place(key[0], key[1], folder, source)
        return folder, source

    def _cleanup_staging(self) -> None:
        staging = self.cfg.staging_dir
        if not staging.is_dir():
            return
        for entry in staging.iterdir():
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            except OSError:
                pass


# --- faze care ruleaza si separat ------------------------------------------

def infer_locations(cfg: Config, db: Database, report: Reporter,
                    dry_run: bool = False) -> int:
    """Muta pozele fara GPS in folderul locatiei deduse din pozele vecine in timp.

    Mutam doar fisiere aflate acum in folderul "fara locatie", deci faza asta nu
    poate strica o sortare facuta pe baza unor coordonate reale.
    """
    unknown = organize.place_folder(None, cfg)
    rows = db.media_without_place(unknown)
    if not rows:
        return 0
    moved = 0
    for row in rows:
        neighbor = db.nearest_located_in_time(row["taken_at"], cfg.infer_window_hours)
        if neighbor is None or not neighbor["place"] or neighbor["place"] == unknown:
            continue
        try:
            taken = datetime.fromisoformat(row["taken_at"])
        except (TypeError, ValueError):
            continue
        new_dir = organize.dest_dir(taken, neighbor["place"], row["kind"], cfg)
        new_rel = new_dir / row["filename"]
        if new_rel.as_posix() == row["rel_path"]:
            continue
        old_abs = cfg.dest / row["rel_path"]
        if not old_abs.is_file():
            report.warn(f"lipseste din backup: {row['rel_path']}")
            continue
        new_abs = unique_path(cfg.dest / new_rel)
        rel_final = new_abs.relative_to(cfg.dest).as_posix()
        report.detail(f"locatie dedusa: {row['rel_path']} -> {rel_final} "
                      f"(dupa {neighbor['filename']})")
        if not dry_run:
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_abs, new_abs)
            db.update_location(
                int(row["id"]), place=neighbor["place"],
                place_source="inferred:time-neighbour",
                lat=neighbor["lat"], lon=neighbor["lon"],
                gps_source="inferred", rel_path=rel_final,
            )
            _prune_empty(old_abs.parent, cfg.dest)
        moved += 1
    if moved and not dry_run:
        db.conn.commit()
    return moved


def _prune_empty(folder: Path, stop_at: Path) -> None:
    """Sterge folderele ramase goale dupa o mutare, fara sa urce peste radacina."""
    try:
        folder = folder.resolve()
        stop_at = stop_at.resolve()
    except OSError:
        return
    while folder != stop_at and stop_at in folder.parents:
        try:
            next(folder.iterdir())
            return
        except StopIteration:
            pass
        except OSError:
            return
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def verify_backup(cfg: Config, db: Database, report: Reporter,
                  deep: bool = False) -> dict:
    """Compara indexul cu ce e efectiv pe SSD."""
    stats = {"checked": 0, "missing": 0, "size_mismatch": 0, "hash_mismatch": 0}
    for row in db.all_media():
        stats["checked"] += 1
        path = cfg.dest / row["rel_path"]
        if not path.is_file():
            stats["missing"] += 1
            report.warn(f"lipseste: {row['rel_path']}")
            continue
        size = path.stat().st_size
        if size != row["size"]:
            stats["size_mismatch"] += 1
            report.warn(f"dimensiune diferita: {row['rel_path']}")
            continue
        if deep:
            if partial_md5(path, size, cfg.partial_chunk) != row["partial_md5"]:
                stats["hash_mismatch"] += 1
                report.warn(f"continut schimbat: {row['rel_path']}")
        report.progress(stats["checked"], None, "verificate")
    return stats


def reindex(cfg: Config, db: Database, geocoder: Geocoder, report: Reporter) -> dict:
    """Reconstruieste indexul din fisierele deja aflate in backup.

    Folositor daca index.db se pierde: nu muta nimic, doar recitieste ce exista.
    """
    stats = {"seen": 0, "added": 0, "already": 0}
    root = cfg.dest
    skip = {d.lower() for d in cfg.skip_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        for name in sorted(filenames):
            if name.endswith(".part"):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            stats["seen"] += 1
            if db.rel_path_exists(rel):
                stats["already"] += 1
                continue
            try:
                size = full.stat().st_size
                fp = Fingerprint(size, partial_md5(full, size, cfg.partial_chunk))
                meta = extract(full)
            except OSError as exc:
                report.warn(f"{rel}: {exc}")
                continue
            place = full.parent.name
            db.insert_media(
                size=fp.size, partial_md5=fp.partial_md5, full_md5=None,
                rel_path=rel, filename=name, kind=meta.kind,
                taken_at=meta.taken_at.isoformat() if meta.taken_at else None,
                taken_at_source=meta.taken_at_source, lat=meta.lat, lon=meta.lon,
                gps_source=meta.gps_source, place=place, place_source="reindex",
                camera_make=meta.make, camera_model=meta.model, group_key=None,
                companion_of=None, source_kind="reindex", source_ref=rel,
                source_name=name, imported_at=utc_now(), run_id=None,
            )
            stats["added"] += 1
            report.progress(stats["seen"], None, "reindexate")
    db.conn.commit()
    return stats
