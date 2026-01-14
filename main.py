import sys
import shutil
import time
from pathlib import Path
from collections import defaultdict

from scanner import *
from db import init_db, get_indexed_file, upsert_file
from hashing import compute_blake3_hash
from backup import build_backup_path, file_exists, copy_file


def main():
    print("iBackup starting...")
    
    # check cmd line args
    if len(sys.argv) < 3:
        print("usage: python main.py <source> <backup>")
        return

    source_path = sys.argv[1]
    source_path = Path(source_path).resolve()
    backup_path = sys.argv[2]
    backup_path = Path(backup_path).resolve()

    if not source_path.exists():
        print("error: source path does not exist")
        return
    if not source_path.is_dir():
        print("error: source  path not a directory")
        return

    if not backup_path.exists():
        print("error: backup path does not exist")
        return
    if not backup_path.is_dir():
        print("error: backup path not a directory")
        return
    
    print("Valid source path:", source_path)
    print("Valid backup path:", backup_path)

    print("partition letter: ", source_path.name[:3])
    # check free space
    free_space = shutil.disk_usage(source_path.drive).free / (1024 ** 3)
    print(f"{source_path.drive} Source free space: {free_space:.2f} GB")

    free_space = shutil.disk_usage(backup_path.drive).free / (1024 ** 3)
    print(f"{backup_path.drive} Backup free space: {free_space:.2f} GB")
    
    # count supported media files
    total_files = 0 
    image_files = 0
    video_files = 0
    media_files = []#scanner
    media_type = ''
    total_size = 0
    image_size = 0
    video_size = 0
    #scan for media files
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