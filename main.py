import sys
import shutil
import time
from pathlib import Path
from collections import defaultdict

from scanner import *
from db import init_db, get_indexed_file, upsert_file
from hashing import compute_blake3_hash
from backup import build_backup_path, file_exists, copy_file


def parse_args():
    if len(sys.argv) < 3:
        print("Usage: python main.py <source_path> <backup_path>")
        sys.exit(1)
    source = Path(sys.argv[1]).resolve()
    backup = Path(sys.argv[2]).resolve()
    return source, backup

def validate_path(path):
    if not path.exists():
        raise ValueError(f"error: {path} does not exist")
    if not path.is_dir():
        raise ValueError(f"error: {path} not a directory")
    print(f"Validated path: {path}")

def print_free_space(path):
    free_space = shutil.disk_usage(path.drive).free / (1024 ** 3)
    print(f"Free space {path.drive}: {free_space:.2f} GB")

def check_free_space(backup_path, media_files):
    print(f"Checking free space on {backup_path} ...")

    required_size = sum(m.size_bytes for m in media_files)
    backup_free = shutil.disk_usage(backup_path.drive).free
    if required_size > backup_free:
        raise ValueError("Not enough free space on backup drive")
    
    print("Sufficient free space on backup drive")

def scan_for_media_files(path):
    print(f"Scanning {path} for media files ...")
    
    media_files = []
    for item in path.rglob('*'):
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

    # logging
    print(f"Collected {len(media_files)} media files\n")    
    print("Files sanity check:")
    for media in media_files[:5]:
        print(media)
    print("\n")

    return media_files

def compute_hashes(media_files):
    print("Computing hashes for media files ...")
    
    total = len(media_files)
    conn = init_db()

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

def find_duplicates(media_files):
    print("Finding duplicate media files ...")
    
    duplicates = defaultdict(list)
    for media in media_files:
        if media.hash is None:
            continue
        duplicates[media.hash].append(media)

    duplicate_groups = {
        h: files for h, files in duplicates.items()
        if len(files) > 1
    }

    print("Duplicate files:")
    if not duplicate_groups:
        print("No duplicates found.")
    else:
        for h, files in duplicate_groups.items():
            print(f"\nHash: {h}")
            for f in files:
                print(f" {f.path}")
    
    return duplicate_groups

def backup_files(media_files, path):
    print(f"Backing up media files to {path}...")

    backup_path = path
    copied = 0
    skipped = 0
    seen_hashes = set() 

    for media in media_files:
        # file already exists in backup
        if file_exists(backup_path, media):
            skipped += 1
            continue
        # duplicate file based on hash
        if media.hash in seen_hashes:
            skipped += 1
            continue
        # build destination path
        dest_dir = build_backup_path(backup_path, media)
        # copy file once
        try:
            copy_file(media, dest_dir)
            seen_hashes.add(media.hash)
            copied += 1
        except Exception as e:
            print("error backing up", media.path, ":", e)

    print(f"Backup complete. copied=[{copied}], skipped=[{skipped}]") #todo: size skipped

def print_stats(media_files):
    total_files = 0 
    image_files = 0
    video_files = 0
    total_size = 0
    image_size = 0
    video_size = 0

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

def main():
    print("iBackup starting...")
    
    source_path, backup_path = parse_args()
    try:
        validate_path(source_path)
        validate_path(backup_path)
    except ValueError as e: 
        print("error:", e)
        sys.exit(1)

    print_free_space(source_path)
    print_free_space(backup_path)

    #scan for media files
    media_files = scan_for_media_files(source_path)

    # check free space
    check_free_space(backup_path, media_files)

    # compute hashes
    compute_hashes(media_files)

    # find duplicates
    duplicates = find_duplicates(media_files)    

    # backup files
    backup_files(media_files, backup_path)

    # calculate stats
    print_stats(media_files)


if __name__ == "__main__":
    start = time.perf_counter()
    main()
    elapsed = time.perf_counter() - start
    print(f"Elapsed time: {elapsed:.2f} seconds")