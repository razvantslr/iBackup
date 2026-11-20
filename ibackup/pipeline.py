"""The engine: scan a source, drop duplicates, file everything into place.

A file's journey:
  1. quick fingerprint (size + partial MD5)
  2. look it up in the index -> if it is already there, it is a duplicate
  3. read its metadata (date + GPS)
  4. GPS -> city name (offline geocoding)
  5. atomic copy into <year>/<month>/<place>/ and a row in the index

Nothing is ever removed from the source. Writes go through a temporary file
followed by a rename, so an interrupted run leaves no half-written files.
"""
from __future__ import annotations

import os
import re
import shutil
import traceback
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
from .util import GB, free_space, human_size, sanitize_component, unique_path

_PRIMARY_ORDER = {"photo": 0, "video": 1, "sidecar": 2, "other": 3}

# How many files may be copied between two disk-usage checks.
SPACE_CHECK_EVERY = 50


class NotEnoughSpace(RuntimeError):
    """Raised to abort the run rather than fill the drive completely."""


@dataclass
class Anchor:
    """What the primary file of a group decided (the photo of a Live Photo)."""
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
    filtered: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def pick_primary(group: list[SourceItem]) -> SourceItem:
    """In a Live Photo the still image leads; the movie and .AAE follow it."""
    return min(group, key=lambda it: (_PRIMARY_ORDER.get(kind_for_name(it.name), 9),
                                      it.name.lower()))


def group_items(items):
    """Group files that share a base name inside the same folder."""
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


def _source_path(item: SourceItem) -> PurePosixPath:
    """Where the file sits inside its source, e.g. DCIM/100APPLE/IMG_0001.HEIC."""
    return PurePosixPath(item.rel_dir, item.name)


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
        self._taken: set[str] = set()           # paths reserved by this run
        self._dry_seen: dict[tuple[int, str], str] = {}  # dry-run duplicates
        self._free_bytes: int | None = None
        self._checked_after: int = 0
        self.unknown_place = organize.place_folder(None, cfg)
        self.allowed_kinds = cfg.allowed_kinds()

    # --- API ---------------------------------------------------------------

    def import_source(self, source: Source) -> Stats:
        mode = "dry-run" if self.dry_run else "import"
        if not self.dry_run:
            self.run_id = self.db.start_run(source.label, str(self.cfg.dest), mode)
            self.cfg.staging_dir.mkdir(parents=True, exist_ok=True)

        self.report.info(f"Source: {source.label}")
        self.report.info(f"Destination: {self.cfg.dest}"
                         + ("  [DRY-RUN, nothing is written]" if self.dry_run else ""))

        try:
            for group in group_items(source.iter_items()):
                if self.limit is not None and self.stats.scanned >= self.limit:
                    self.report.info(f"Stopping at the limit of {self.limit} files.")
                    break
                self._process_group(source, group)
                self.report.progress(self.stats.scanned, source.count_hint(),
                                     f"{self.stats.imported} new, "
                                     f"{self.stats.duplicates} duplicates")
        except NotEnoughSpace as exc:
            self.stats.errors += 1
            self.report.error(f"Stopped: {exc}")
            self.report.info("Everything copied so far is indexed and valid; "
                             "free some space and run the same command again.")
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

    # --- group -------------------------------------------------------------

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
        except NotEnoughSpace:
            raise  # aborts the whole run, not just this file
        except Exception as exc:
            self.stats.errors += 1
            message = f"{item.ref}: {type(exc).__name__}: {exc}"
            self.report.error(message)
            self.report.detail(traceback.format_exc().rstrip())
            if not self.dry_run and self.run_id is not None:
                try:
                    self.db.log_error(self.run_id, item.ref, message)
                except Exception:
                    pass
            return None

    # --- single file -------------------------------------------------------

    def _process_item(self, source: Source, item: SourceItem,
                      group_key: str | None, anchor: Anchor | None) -> Anchor | None:
        self.stats.scanned += 1
        chunk = self.cfg.partial_chunk
        fp, decided = self._recall(item)
        if decided is not None:
            # We already decided about this exact file on an earlier run, so
            # there is nothing to read: it is a duplicate of that row. The
            # remembered row also tells us its kind, so the filter can still be
            # honoured in the statistics.
            if decided["kind"] not in self.allowed_kinds:
                self.stats.filtered += 1
                self.report.detail(
                    f"filtered out ({decided['kind']}): {_source_path(item)}")
                return None
            return self._on_duplicate(item, fp, decided)

        if fp is None and not source.supports_random_read:
            # The only way to fingerprint this source is to fetch the file, so we
            # hold on to it and reuse it for the metadata read and the copy.
            with source.localize(item, self.cfg.staging_dir) as local:
                size = local.path.stat().st_size
                fp = Fingerprint(size, partial_md5(local.path, size, chunk))
                matched = self._find_duplicate(item, fp, local.path, source)
                if matched is not None:
                    return self._on_duplicate(item, fp, matched)
                return self._ingest(item, local, fp, group_key, anchor)

        if fp is None:
            fp = Fingerprint(
                size=item.size,
                partial_md5=partial_md5_from_reader(
                    lambda off, length: source.read_range(item, off, length),
                    item.size, chunk,
                ),
            )

        matched = self._find_duplicate(item, fp, source.local_path(item), source)
        if matched is not None:
            return self._on_duplicate(item, fp, matched)
        with source.localize(item, self.cfg.staging_dir) as local:
            return self._ingest(item, local, fp, group_key, anchor)

    def _recall(self, item: SourceItem):
        """(fingerprint, already-matched row) remembered from an earlier run."""
        if not self.cfg.use_source_cache or item.mtime is None:
            return None, None
        row = self.db.cached_source(item.kind, item.ref, item.size, item.mtime)
        if row is None:
            return None, None
        fp = Fingerprint(item.size, row["partial_md5"])
        media_id = row["media_id"]
        if media_id is None:
            return fp, None
        # The backup may have changed since; only trust a row still present.
        return fp, self.db.media_by_id(int(media_id))

    def _remember(self, item: SourceItem, fp: Fingerprint,
                  media_id: int | None) -> None:
        if not self.cfg.use_source_cache or self.dry_run or item.mtime is None:
            return
        self.db.cache_source(item.kind, item.ref, fp.size, item.mtime,
                             fp.partial_md5, media_id)

    def _find_duplicate(self, item: SourceItem, fp: Fingerprint,
                        local_path: Path | None, source: Source):
        """The indexed row with the same content, or None."""
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

        # Confirming needs the whole file. If the source is the phone, that would
        # mean downloading the photo just to learn we already have it, so we only
        # do it when the user asked for --strict-dedup.
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
        return None  # same partial fingerprint, different content: not a duplicate

    def _on_duplicate(self, item: SourceItem, fp: Fingerprint, row) -> Anchor | None:
        self.stats.duplicates += 1
        self.report.detail(f"duplicate: {_source_path(item)} -> {row['rel_path']}")
        if not self.dry_run:
            self.db.record_duplicate(self.run_id, item, int(row["id"]),
                                     fp.size, fp.partial_md5)
            self._remember(item, fp, int(row["id"]))
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

    # --- writing to the destination ----------------------------------------

    def _ingest(self, item: SourceItem, local: Localized, fp: Fingerprint,
                group_key: str | None, anchor: Anchor | None) -> Anchor | None:
        kind = kind_for_name(item.name)
        if kind == "sidecar":
            meta = MediaMeta(kind="sidecar")
        else:
            meta = extract(local.path,
                           logical_path=_source_path(item),
                           use_mtime=not local.is_temp,
                           detect_screenshots=self.cfg.detect_screenshots)
            if meta.kind == "screenshot":
                self.report.detail(
                    f"screenshot ({meta.screenshot_reason}): {_source_path(item)}")

        # Screenshots can only be told apart once the metadata has been read, so
        # this filter runs here rather than during enumeration. On a phone that
        # means the file was fetched and is now discarded.
        if meta.kind not in self.allowed_kinds:
            self.stats.filtered += 1
            self.report.detail(f"filtered out ({meta.kind}): {_source_path(item)}")
            self._remember(item, fp, None)
            return None

        if meta.taken_at is None and item.mtime:
            meta.taken_at = datetime.fromtimestamp(item.mtime)
            meta.taken_at_source = "source-mtime"

        if anchor is not None:
            # Paired files go next to their photo, carrying the same data.
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

        # Sidecars have no metadata of their own, so counting them as gaps would
        # only inflate the statistics.
        if meta.kind in ("photo", "video"):
            if not meta.taken_at_source.startswith(("exif", "qt")):
                self.stats.no_exif_date += 1
            if lat is None:
                self.stats.no_gps += 1

        filename = organize.target_filename(item.name, taken_at, self.cfg, stem_override)
        dest_abs = unique_path(self.cfg.dest / rel_dir / filename, self._taken)
        rel_final = dest_abs.relative_to(self.cfg.dest).as_posix()
        self._taken.add(str(dest_abs).lower())

        date_note = (f"date {taken_at:%Y-%m-%d %H:%M} ({meta.taken_at_source or '?'})"
                     if taken_at else "no date")
        why = (f"{meta.kind}; {date_note}; "
               f"place {place} ({place_source or 'no GPS'})")
        if anchor is not None:
            why += "; paired with its photo"

        if self.dry_run:
            self.stats.imported += 1
            self.stats.bytes_copied += fp.size
            self._dry_seen[fp.as_key()] = rel_final
            self.report.detail(f"would copy: {_source_path(item)} -> {rel_final} [{why}]")
        else:
            self._ensure_space(fp.size)
            self._place_file(local, dest_abs, fp, item)
            self.stats.imported += 1
            self.stats.bytes_copied += fp.size
            self.report.detail(f"copied: {_source_path(item)} -> {rel_final} [{why}]")
            media_id = self.db.insert_media(
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
            self._remember(item, fp, media_id)

        return Anchor(
            dest_dir=PurePosixPath(rel_final).parent,
            stem=PurePosixPath(rel_final).stem, taken_at=taken_at, place=place,
            place_source=place_source, lat=lat, lon=lon, gps_source=gps_source,
        )

    def _place_file(self, local: Localized, dest_abs: Path, fp: Fingerprint,
                    item: SourceItem) -> None:
        """Atomic write: .part first, then rename. We check what landed on disk."""
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        temp = dest_abs.with_name(dest_abs.name + ".part")
        if temp.exists():
            temp.unlink()
        if local.is_temp:
            os.replace(local.path, temp)   # staging is on the same volume: instant
            local.claimed = True
        else:
            shutil.copy2(local.path, temp)

        if self.cfg.verify_after_copy:
            written = temp.stat().st_size
            check = partial_md5(temp, written, self.cfg.partial_chunk)
            if written != fp.size or check != fp.partial_md5:
                temp.unlink(missing_ok=True)
                raise IOError(
                    f"verification failed after copying {item.name}: "
                    f"{written} bytes vs {fp.size} expected"
                )
        if item.mtime:
            try:
                os.utime(temp, (item.mtime, item.mtime))
            except OSError:
                pass
        os.replace(temp, dest_abs)

    def _ensure_space(self, needed: int) -> None:
        """Stop before the drive fills up, without stat-ing it for every file.

        v0.1 summed every source file and compared once, which both refused runs
        that would have fitted (most files are usually already backed up) and
        needed a full pre-pass - expensive over MTP. Checking as we go costs one
        syscall per 50 files and reflects what is actually being written.
        """
        reserve = int(max(0.0, self.cfg.min_free_gb) * GB)
        stale = (self._free_bytes is None
                 or self.stats.imported - self._checked_after >= SPACE_CHECK_EVERY)
        if stale:
            self._free_bytes = free_space(self.cfg.dest)
            self._checked_after = self.stats.imported

        if self._free_bytes - needed < reserve:
            # The cached figure may be out of date; confirm before giving up.
            self._free_bytes = free_space(self.cfg.dest)
            self._checked_after = self.stats.imported
            if self._free_bytes - needed < reserve:
                raise NotEnoughSpace(
                    f"the backup drive is full: {human_size(self._free_bytes)} "
                    f"free, {human_size(needed)} needed, and "
                    f"{human_size(reserve)} must stay free "
                    f"(safety.min_free_gb). Copied so far: {self.stats.imported}."
                )
        self._free_bytes -= needed

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


# --- phases that can also run on their own ---------------------------------

def _parse_taken(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _relocate_file(cfg: Config, report: Reporter, row, new_rel: PurePosixPath,
                   reserved: set[str], dry_run: bool) -> str | None:
    """Move an indexed file into a new folder; returns its new relative path.

    Shared by the three phases that rearrange an existing backup. None means the
    file was not where the index said it would be, and the caller should count
    that as missing and leave the row alone.
    """
    old_abs = cfg.dest / row["rel_path"]
    if not old_abs.is_file():
        report.warn(f"missing from the backup: {row['rel_path']}")
        return None
    new_abs = unique_path(cfg.dest / new_rel, reserved)
    rel_final = new_abs.relative_to(cfg.dest).as_posix()
    reserved.add(str(new_abs).lower())
    if not dry_run:
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_abs, new_abs)
        _prune_empty(old_abs.parent, cfg.dest)
    return rel_final


def infer_locations(cfg: Config, db: Database, report: Reporter,
                    dry_run: bool = False) -> int:
    """Move photos without GPS into the location guessed from their neighbours.

    Only files currently sitting in the "no location" folder are moved, so this
    phase can never disturb a placement based on real coordinates.
    """
    unknown = organize.place_folder(None, cfg)
    rows = db.media_without_place(unknown)
    if not rows:
        return 0
    moved = 0
    reserved: set[str] = set()
    for row in rows:
        # Screenshots are filed by kind, not by place, so a guessed location
        # would not change where they live - and a screenshot taken during a
        # trip is not evidence of anything anyway.
        if row["kind"] == "screenshot" and cfg.screenshot_dir:
            continue
        neighbor = db.nearest_located_in_time(row["taken_at"], cfg.infer_window_hours)
        if neighbor is None or not neighbor["place"] or neighbor["place"] == unknown:
            continue
        taken = _parse_taken(row["taken_at"])
        if taken is None:
            continue
        new_rel = (organize.dest_dir(taken, neighbor["place"], row["kind"], cfg)
                   / row["filename"])
        if new_rel.as_posix() == row["rel_path"]:
            continue
        rel_final = _relocate_file(cfg, report, row, new_rel, reserved, dry_run)
        if rel_final is None:
            continue
        report.detail(f"location guessed: {row['rel_path']} -> {rel_final} "
                      f"(after {neighbor['filename']})")
        if not dry_run:
            db.update_location(
                int(row["id"]), place=neighbor["place"],
                place_source="inferred:time-neighbour",
                lat=neighbor["lat"], lon=neighbor["lon"],
                gps_source="inferred", rel_path=rel_final,
            )
        moved += 1
    if moved and not dry_run:
        db.conn.commit()
    return moved


def _prune_empty(folder: Path, stop_at: Path) -> None:
    """Remove folders left empty by a move, without climbing past the root."""
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


def reorganize(cfg: Config, db: Database, report: Reporter,
               dry_run: bool = False) -> dict:
    """Re-apply the current layout rules to everything already in the backup.

    This is what makes the layout settings reversible: change screenshot_dir,
    language or video_subdir, run this, and the tree catches up. Only the folder
    is recomputed - filenames are left alone, so organize.rename_with_timestamp
    still applies to newly imported files only.
    """
    stats = {"checked": 0, "moved": 0, "missing": 0}
    reserved: set[str] = set()
    for row in db.iter_media():
        stats["checked"] += 1
        taken = _parse_taken(row["taken_at"])
        new_dir = organize.dest_dir(taken, row["place"], row["kind"], cfg)
        new_rel = new_dir / row["filename"]
        if new_rel.as_posix() == row["rel_path"]:
            continue

        rel_final = _relocate_file(cfg, report, row, new_rel, reserved, dry_run)
        if rel_final is None:
            stats["missing"] += 1
            continue
        report.detail(f"{row['rel_path']} -> {rel_final}")
        if not dry_run:
            db.set_rel_path(int(row["id"]), rel_final)
        stats["moved"] += 1
        report.progress(stats["checked"], None, "checked")

    if not dry_run:
        db.conn.commit()
    return stats


def regeocode(cfg: Config, db: Database, geocoder: Geocoder, report: Reporter,
              dry_run: bool = False) -> dict:
    """Recompute every location from its stored coordinates and move what moved.

    A place name is written into the index once, at import time, so installing a
    better dataset, widening the radius or adding an alias would otherwise only
    affect future imports - the photos already filed under GPS_45.3N_25.5E would
    stay there forever. This is the geocoding counterpart of reorganize.
    """
    stats = {"checked": 0, "changed": 0, "moved": 0, "missing": 0}
    if not dry_run:
        # The cache holds answers from the settings we are replacing.
        db.clear_place_cache()

    resolved: dict[tuple[float, float], tuple[str, str]] = {}
    reserved: set[str] = set()
    for row in db.iter_media():
        stats["checked"] += 1
        lat, lon = row["lat"], row["lon"]
        if lat is None or lon is None:
            continue

        key = geocoder.cluster_key(lat, lon)
        if key not in resolved:
            name, source = geocoder.place_name(lat, lon)
            resolved[key] = (organize.place_folder(name, cfg), source)
            if not dry_run:
                db.cache_place(key[0], key[1], *resolved[key])
        place, place_source = resolved[key]
        if place == row["place"]:
            continue

        stats["changed"] += 1
        report.detail(f"{row['place']} -> {place}: {row['rel_path']}")
        taken = _parse_taken(row["taken_at"])
        new_rel = organize.dest_dir(taken, place, row["kind"], cfg) / row["filename"]
        rel_final = row["rel_path"]
        if new_rel.as_posix() != rel_final:
            moved_to = _relocate_file(cfg, report, row, new_rel, reserved, dry_run)
            if moved_to is None:
                stats["missing"] += 1
                continue
            rel_final = moved_to
            stats["moved"] += 1
        if not dry_run:
            db.update_location(
                int(row["id"]), place=place, place_source=place_source,
                lat=lat, lon=lon, gps_source=row["gps_source"] or "",
                rel_path=rel_final,
            )
        report.progress(stats["checked"], None, "checked")

    if not dry_run:
        db.conn.commit()
    return stats


def verify_backup(cfg: Config, db: Database, report: Reporter,
                  deep: bool = False) -> dict:
    """Compare the index against what is actually on the SSD."""
    stats = {"checked": 0, "missing": 0, "size_mismatch": 0, "hash_mismatch": 0}
    for row in db.all_media():
        stats["checked"] += 1
        path = cfg.dest / row["rel_path"]
        if not path.is_file():
            stats["missing"] += 1
            report.warn(f"missing: {row['rel_path']}")
            continue
        size = path.stat().st_size
        if size != row["size"]:
            stats["size_mismatch"] += 1
            report.warn(f"different size: {row['rel_path']}")
            continue
        if deep:
            if partial_md5(path, size, cfg.partial_chunk) != row["partial_md5"]:
                stats["hash_mismatch"] += 1
                report.warn(f"content changed: {row['rel_path']}")
        report.progress(stats["checked"], None, "checked")
    return stats


def _place_from_layout(rel: str, kind: str, cfg: Config) -> str | None:
    """The place folder of a file in the backup: the one right under its month."""
    if kind == "screenshot" and cfg.screenshot_dir:
        return None
    dirs = list(PurePosixPath(rel).parent.parts)
    if (kind == "video" and cfg.video_subdir and dirs
            and dirs[-1] == sanitize_component(cfg.video_subdir, fallback="Video")):
        dirs.pop()
    if len(dirs) == 2 and dirs[0] == organize.NO_DATE_FOLDER:
        return dirs[1]
    if (len(dirs) == 3 and re.fullmatch(r"\d{4}", dirs[0])
            and re.fullmatch(r"\d{2}_.+", dirs[1])):
        return dirs[2]
    return None


def reindex(cfg: Config, db: Database, geocoder: Geocoder, report: Reporter) -> dict:
    """Rebuild the index from the files already sitting in the backup.

    Useful if index.db is lost: nothing is moved, the tree is only re-read.
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
                meta = extract(full, logical_path=PurePosixPath(rel))
            except OSError as exc:
                report.warn(f"{rel}: {exc}")
                continue
            place = _place_from_layout(rel, meta.kind, cfg)
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
            report.progress(stats["seen"], None, "reindexed")
    db.conn.commit()
    return stats
