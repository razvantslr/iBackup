import sys
import shutil
import time
import sqlite3

from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from blake3 import blake3
from collections import defaultdict
from datetime import datetime

# constants.py
SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.heic'}
SUPPORTED_VIDEO_EXT = {'.mp4', '.mov'}
SUPPORTED_MEDIA_EXT = SUPPORTED_IMAGE_EXT | SUPPORTED_VIDEO_EXT

#
@dataclass
class MediaFile:
    path: str
    extension: str
    media_type: str # 'image' | 'video' 
    size_bytes: int
    hash: Optional[str] = None

def bytes_to_gb(num_bytes: int) -> float:
    return num_bytes / (1024 ** 3)

def compute_blake3_hash(file_path: str, chunk_size=1024 * 1024) -> str:
    hasher = blake3()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

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

def get_file_date(path: str):
    stat = Path(path).stat()
    return datetime.fromtimestamp(stat.st_mtime)

def build_backup_path(base_dest, media):
    date = get_file_date(media.path)
    year = date.strftime("%Y")
    month = f"{date.month:02d}"

    dest_dir = Path(base_dest) / year / month
    dest_dir.mkdir(parents=True, exist_ok=True)

    return dest_dir

def file_exists(dest_dir, media):
    #dest_dir = Path(dest_dir)
    return (dest_dir / Path(media.path).name).exists()

def copy_file(media, dest_dir):
    src = Path(media.path)
    dst = dest_dir / src.name
    shutil.copy2(src,dst)

def main():
    print("iBackup starting...")
    
    # check cmd line args
    if len(sys.argv) < 2:
        print("usage: python main.py <backup_dir>")
        return
    
    # check c free space
    free_space = shutil.disk_usage("c:/").free / (1024 ** 3)
    print(f"c:/ Free space: {free_space:.2f} GB")
    
    backup_path = sys.argv[1]
    backup_path = Path(backup_path).resolve()

    # validate backup path
    if not backup_path.exists():
        print("error: path does not exist")
        return

    if not backup_path.is_dir():
        print("error: not a directory")
        return
    
    print("Valid backup path:", backup_path)

    # print contents of backup
    # print("contents of backup dir:")
    # for item in backup_path.iterdir():
    #     if item.is_file():
    #         print("FILE: " + item.name)
    #     elif item.is_dir():
    #         print("DIR: " + item.name)
    
    # count supported media files
    total_files = 0 
    image_files = 0
    video_files = 0
    media_files = []
    media_type = ''
    total_size = 0
    image_size = 0
    video_size = 0

    for item in backup_path.rglob('*'):
        if not item.is_file():
            continue

        ext = item.suffix.lower()
        if ext not in SUPPORTED_MEDIA_EXT:
            continue

        if ext in SUPPORTED_IMAGE_EXT:
            media_type = 'image'
        elif ext in SUPPORTED_VIDEO_EXT:
            media_type = 'video'

        media = MediaFile(
            path=str(item),
            extension=ext,
            media_type=media_type,
            size_bytes=item.stat().st_size,
        )
        media_files.append(media)

    print(f"\nCollected {len(media_files)} media files\n")    
    print("\nFiles sanity check:")
    for media in media_files[:5]:
        print(media)

    # compute hashes
    print("\nComputing hashes for media files ...")
    
    total = len(media_files)
    conn = init_db()

    #for media in media_files:
    for index, media in enumerate(media_files, start=1):
        stat = Path(media.path).stat()
        indexed = get_indexed_file(conn, media.path)
        if indexed:
            old_size, old_mtime, old_hash = indexed
            if old_size == stat.st_size and old_mtime == stat.st_mtime:
                media.hash = old_hash
                continue # skip re-hashing
        
        try:
            media.hash = compute_blake3_hash(media.path)
        except Exception as e:
            print("error computhing hash for", media.path, ":", e)
            continue

        upsert_file(conn, media)

        if index % 10 == 0 or index == total:
            print(f"[{index}/{total}] hashed")
    
    print("\nHash sanity check:")
    for media in media_files[:5]:
        print(media.hash, media.path)

    # find duplicates
    duplicates = defaultdict(list)
    for media in media_files:
        if media.hash is None:
            continue
        duplicates[media.hash].append(media)
    duplicate_groups = {
        h: files for h, files in duplicates.items()
        if len(files) > 1
    }

    print("\nDuplicates files:")
    if not duplicate_groups:
        print("no dulicates found.")
    else:
        for h, files in duplicate_groups.items():
            print(f"\nHash: {h}")
            for f in files:
                print(f" {f.path}")

    # backup files
    backup_path = Path(f"C:/Projects/iBackup/TEST/backup/")
    copied = 0
    skipped = 0
    seen_hashes = set() 

    for media in media_files:
        if file_exists(backup_path, media):
            skipped += 1
            continue
        if media.hash in seen_hashes:
            skipped += 1
            continue

        dest_dir = build_backup_path(backup_path, media)
        try:
            copy_file(media, dest_dir)
            seen_hashes.add(media.hash)
            copied += 1
        except Exception as e:
            print("error backing up", media.path, ":", e)

    print(f"\nBackup complete. copied=[{copied}], skipped=[{skipped}]")
          

    # calculate stats
    for media in media_files:
        total_files += 1 
        total_size += media.size_bytes

        if media.media_type == 'image':
            image_files += 1
            image_size += media.size_bytes
        elif media.media_type == 'video':
            video_files += 1
            video_size += media.size_bytes 

    print("\nScan summary:")
    print(f"Total media files : {total_files}")
    print(f"Images           : {image_files}")
    print(f"Videos           : {video_files}")

    print(f"\nSize summary:")
    print(f"Total size :  {bytes_to_gb(total_size):.2f} GB")
    print(f"Images size :  {bytes_to_gb(image_size):.2f} GB")
    print(f"Videos size :  {bytes_to_gb(video_size):.2f} GB")


if __name__ == "__main__":
    start = time.perf_counter()
    main()
    elapsed = time.perf_counter() - start
    print(f"Elapsed time: {elapsed:.2f} seconds")