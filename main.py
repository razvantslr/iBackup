import sys
import shutil
import time
import argparse
from pathlib import Path
from collections import defaultdict

from scanner import scan_for_media_files, bytes_to_gb, MediaType
from db import init_db, get_indexed_file, upsert_file
from hashing import compute_blake3_hash
from backup import build_backup_path, file_exists, copy_file


def parse_args():
    parser = argparse.ArgumentParser(
        prog="iBackup",
        description="A tool to backup and organize media files."
    )

    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to the source directory containing media files."
    )

    parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="Path to the backup directory where media files will be copied."
    )

    parser.add_argument(
        "--only",
        choices=[type.value for type in MediaType],
        help="Only process media files of the specified type."
    )

    #todo: add --exclude and --include options

    parser.add_argument(
        "--dry-run",
        action="store_true", 
        help="Perform a trial run without making any changes."
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output."
    )

    args = parser.parse_args()

    args.source = args.source.resolve() # eg. from "./media" to "C:/media"
    args.backup = args.backup.resolve()

    # todo: add after exclude and include options implementation
    # validation only one filter mode
    #modes = [args.only, args.include, args.exclude]
    #if sum(mode is not None for mode in modes) > 1: # todo: refactor more readable like this: 
    #    parser.error("Only one of --only, --include, or --exclude can be specified.")

    return args

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

    if not media_files:
        raise ValueError("No media files to process.")

    required_size = sum(m.size_bytes for m in media_files)
    backup_free = shutil.disk_usage(backup_path.drive).free
    if required_size > backup_free:
        raise ValueError("Not enough free space on backup drive")
    
    print("Sufficient free space on backup drive")

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

        if media.type == MediaType.IMAGE or media.type == MediaType.SCREENSHOT:
            image_files += 1
            image_size += media.size_bytes
        elif media.type == MediaType.VIDEO:
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

def apply_media_folders(media_files, args):
    if args.only:
        only_type = MediaType(args.only) # convert string to MediaType
        return [m for m in media_files if m.type == only_type] # filter by type

    # todo: implement include and exclude filters

    return media_files

def classify_media_files(media_files):

    return

def main():
    print("iBackup starting...")
    
    args = parse_args()
    try:
        validate_path(args.source)
        validate_path(args.backup)
    except ValueError as e: 
        print("error:", e)
        sys.exit(1)

    print_free_space(args.source)
    print_free_space(args.backup)

    #scan for media files
    media_files = scan_for_media_files(args.source)
    #classify_media_files(media_files)

    media_files = apply_media_folders(media_files, args)

    # check free space
    check_free_space(args.backup, media_files)
    # compute hashes
    compute_hashes(media_files)

    # find duplicates
    duplicates = find_duplicates(media_files)    

    # backup files
    backup_files(media_files, args.backup)

    # calculate stats
    print_stats(media_files)


if __name__ == "__main__":
    start = time.perf_counter()
    main()
    elapsed = time.perf_counter() - start
    print(f"Elapsed time: {elapsed:.2f} seconds")