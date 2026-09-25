"""The SQLite index that remembers what is already in the backup.

The database is the source of truth for deduplication and for knowing where
each file ended up. It lives in <backup>/.ibackup/index.db, so it travels with
the SSD. If it is ever lost, `reindex` rebuilds it from the files on disk.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id              INTEGER PRIMARY KEY,
    size            INTEGER NOT NULL,
    partial_md5     TEXT    NOT NULL,
    full_md5        TEXT,
    rel_path        TEXT    NOT NULL UNIQUE,
    filename        TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    taken_at        TEXT,
    taken_at_source TEXT,
    lat             REAL,
    lon             REAL,
    gps_source      TEXT,
    place           TEXT,
    place_source    TEXT,
    camera_make     TEXT,
    camera_model    TEXT,
    group_key       TEXT,
    companion_of    INTEGER,
    source_kind     TEXT,
    source_ref      TEXT,
    source_name     TEXT,
    imported_at     TEXT,
    run_id          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_media_fingerprint ON media(size, partial_md5);
CREATE INDEX IF NOT EXISTS idx_media_taken       ON media(taken_at);
CREATE INDEX IF NOT EXISTS idx_media_place       ON media(place);
CREATE INDEX IF NOT EXISTS idx_media_group       ON media(group_key);

CREATE TABLE IF NOT EXISTS duplicates (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER,
    source_kind      TEXT,
    source_ref       TEXT,
    source_name      TEXT,
    size             INTEGER,
    partial_md5      TEXT,
    matched_media_id INTEGER,
    seen_at          TEXT
);

CREATE TABLE IF NOT EXISTS source_cache (
    source_kind TEXT    NOT NULL,
    source_ref  TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    partial_md5 TEXT    NOT NULL,
    media_id    INTEGER,
    seen_at     TEXT,
    PRIMARY KEY (source_kind, source_ref)
);

CREATE TABLE IF NOT EXISTS place_cache (
    lat    REAL NOT NULL,
    lon    REAL NOT NULL,
    place  TEXT NOT NULL,
    source TEXT,
    PRIMARY KEY (lat, lon)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    source      TEXT,
    dest        TEXT,
    mode        TEXT,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER,
    source_ref TEXT,
    message    TEXT,
    at         TEXT
);
"""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_info(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- runs --------------------------------------------------------------

    def start_run(self, source: str, dest: str, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, source, dest, mode) VALUES (?, ?, ?, ?)",
            (_now(), source, dest, mode),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE id = ?",
            (_now(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def log_error(self, run_id: int | None, source_ref: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO errors(run_id, source_ref, message, at) VALUES (?, ?, ?, ?)",
            (run_id, source_ref, message[:2000], _now()),
        )

    # --- deduplication -----------------------------------------------------

    def find_by_fingerprint(self, size: int, partial_md5: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM media WHERE size = ? AND partial_md5 = ?",
            (size, partial_md5),
        ))

    def set_full_md5(self, media_id: int, full_md5: str) -> None:
        self.conn.execute("UPDATE media SET full_md5 = ? WHERE id = ?", (full_md5, media_id))

    def record_duplicate(self, run_id: int | None, item, matched_id: int,
                         size: int, partial_md5: str) -> None:
        self.conn.execute(
            "INSERT INTO duplicates(run_id, source_kind, source_ref, source_name,"
            " size, partial_md5, matched_media_id, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, item.kind, item.ref, item.name, size, partial_md5,
             matched_id, _now()),
        )

    # --- insert / update ---------------------------------------------------

    def insert_media(self, **kw) -> int:
        columns = (
            "size", "partial_md5", "full_md5", "rel_path", "filename", "kind",
            "taken_at", "taken_at_source", "lat", "lon", "gps_source", "place",
            "place_source", "camera_make", "camera_model", "group_key",
            "companion_of", "source_kind", "source_ref", "source_name",
            "imported_at", "run_id",
        )
        values = [kw.get(col) for col in columns]
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO media({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)

    def update_location(self, media_id: int, *, place: str, place_source: str,
                        lat: float | None, lon: float | None,
                        gps_source: str, rel_path: str) -> None:
        self.conn.execute(
            "UPDATE media SET place = ?, place_source = ?, lat = ?, lon = ?,"
            " gps_source = ?, rel_path = ? WHERE id = ?",
            (place, place_source, lat, lon, gps_source, rel_path, media_id),
        )

    def set_rel_path(self, media_id: int, rel_path: str) -> None:
        self.conn.execute("UPDATE media SET rel_path = ? WHERE id = ?",
                          (rel_path, media_id))

    def rel_path_exists(self, rel_path: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM media WHERE rel_path = ? LIMIT 1", (rel_path,)
        ).fetchone()
        return row is not None

    def delete_media(self, media_id: int) -> None:
        self.conn.execute("DELETE FROM media WHERE id = ?", (media_id,))

    # --- source fingerprint cache ------------------------------------------

    def cached_source(self, source_kind: str, source_ref: str,
                      size: int, mtime: float):
        """What we learned last time about this exact source file.

        Matching on size AND mtime is the rsync heuristic: cheap, and wrong only
        if a file changed while keeping both. --no-cache bypasses it. The stored
        media_id records the decision we reached, so a re-run of the same folder
        does not have to read the file again to reach it a second time.
        """
        return self.conn.execute(
            "SELECT partial_md5, media_id FROM source_cache WHERE source_kind = ?"
            " AND source_ref = ? AND size = ? AND mtime = ?",
            (source_kind, source_ref, size, mtime),
        ).fetchone()

    def cache_source(self, source_kind: str, source_ref: str, size: int,
                     mtime: float, partial_md5: str,
                     media_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_cache(source_kind, source_ref, size,"
            " mtime, partial_md5, media_id, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_kind, source_ref, size, mtime, partial_md5, media_id, _now()),
        )

    def media_by_id(self, media_id: int):
        return self.conn.execute(
            "SELECT * FROM media WHERE id = ?", (media_id,)
        ).fetchone()

    # --- geocoding cache ---------------------------------------------------

    def cached_place(self, lat: float, lon: float):
        row = self.conn.execute(
            "SELECT place, source FROM place_cache WHERE lat = ? AND lon = ?",
            (lat, lon),
        ).fetchone()
        return (row["place"], row["source"]) if row else None

    def clear_place_cache(self) -> None:
        """Drop the cached lookups; used when the geocoding settings change."""
        self.conn.execute("DELETE FROM place_cache")

    def cache_place(self, lat: float, lon: float, place: str, source: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO place_cache(lat, lon, place, source)"
            " VALUES (?, ?, ?, ?)",
            (lat, lon, place, source),
        )

    # --- queries used when guessing a location -----------------------------

    def media_without_place(self, unknown_place: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM media WHERE lat IS NULL AND taken_at IS NOT NULL"
            " AND (place = ? OR place IS NULL) ORDER BY taken_at",
            (unknown_place,),
        ))

    def nearest_located_in_time(self, taken_at: str, window_hours: float):
        """The closest photo in time that does have GPS, within the window."""
        try:
            pivot = datetime.fromisoformat(taken_at)
        except ValueError:
            return None
        delta = timedelta(hours=window_hours)
        low, high = (pivot - delta).isoformat(), (pivot + delta).isoformat()
        before = self.conn.execute(
            "SELECT * FROM media WHERE lat IS NOT NULL AND taken_at IS NOT NULL"
            " AND taken_at BETWEEN ? AND ? ORDER BY taken_at DESC LIMIT 1",
            (low, taken_at),
        ).fetchone()
        after = self.conn.execute(
            "SELECT * FROM media WHERE lat IS NOT NULL AND taken_at IS NOT NULL"
            " AND taken_at BETWEEN ? AND ? ORDER BY taken_at ASC LIMIT 1",
            (taken_at, high),
        ).fetchone()
        best, best_gap = None, None
        for row in (before, after):
            if row is None:
                continue
            try:
                gap = abs((datetime.fromisoformat(row["taken_at"]) - pivot).total_seconds())
            except ValueError:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = row, gap
        return best

    def group_members(self, group_key: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM media WHERE group_key = ?", (group_key,)
        ))

    # --- reporting ---------------------------------------------------------

    def all_media(self):
        return self.conn.execute("SELECT * FROM media ORDER BY rel_path")

    def count_media(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0])

    def stats(self) -> dict:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes,"
            " SUM(CASE WHEN kind = 'photo' THEN 1 ELSE 0 END) AS photos,"
            " SUM(CASE WHEN kind = 'video' THEN 1 ELSE 0 END) AS videos,"
            " SUM(CASE WHEN kind = 'screenshot' THEN 1 ELSE 0 END) AS screenshots,"
            " SUM(CASE WHEN lat IS NOT NULL THEN 1 ELSE 0 END) AS with_gps,"
            " MIN(taken_at) AS first_taken, MAX(taken_at) AS last_taken"
            " FROM media"
        ).fetchone()
        out = dict(cur)
        out["places"] = [
            dict(row) for row in self.conn.execute(
                # Screenshots are filed by kind, so their place column is
                # meaningless and would distort the tally.
                "SELECT place, COUNT(*) AS n FROM media WHERE place IS NOT NULL"
                " AND kind != 'screenshot' GROUP BY place ORDER BY n DESC LIMIT 20"
            )
        ]
        out["years"] = [
            dict(row) for row in self.conn.execute(
                "SELECT substr(taken_at, 1, 4) AS year, COUNT(*) AS n FROM media"
                " WHERE taken_at IS NOT NULL GROUP BY year ORDER BY year"
            )
        ]
        out["duplicates_seen"] = int(
            self.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        )
        return out
