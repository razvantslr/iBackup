import sqlite3
from pathlib import Path


def init_db(db_path="ibackup.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
                CREATE TABLE IF NOT EXISTS media_index (
                    path TEXT PRIMARY KEY,
                    size_bytes INTEGER,
                    mtime REAL,
                    hash TEXT
                    )
                ''')
    conn.commit()
    return conn

def get_indexed_file(conn, path):
    cur = conn.cursor()
    cur.execute(
        "SELECT size_bytes, mtime, hash FROM media_index WHERE path = ?",
        (path,)
    )
    return cur.fetchone()

def upsert_file(conn, media):
    stat = Path(media.path).stat()
    conn.execute("""
        INSERT INTO media_index (path, size_bytes, mtime, hash)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size_bytes=excluded.size_bytes,
            mtime=excluded.mtime,
            hash=excluded.hash
    """, (
        media.path,
        stat.st_size,
        stat.st_mtime,
        media.hash
    ))
    conn.commit()
