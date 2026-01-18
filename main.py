import logging
import sys
import shutil
import time
import argparse
from pathlib import Path
from collections import defaultdict

from scanner import scan_for_media_files, bytes_to_gb, MediaType
from db import init_db, get_indexed_file, upsert_file
from hashing import compute_blake3_hash, compute_hashes
from backup import build_backup_path, file_exists, copy_file
from logger import logger_init
from duplicate import find_duplicates

logger = logging.getLogger("iBackup")

def parse_args():
    
    parser = argparse.ArgumentParser(
        prog="iBackup",
        description="A tool to backup and organize media files."
    )

    # Required arguments
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

    # Filter options
    parser.add_argument(
        "--only",
        choices=[type.value for type in MediaType],
        help="Filter: Only the specified type will be processed."
    )

    parser.add_argument(
        "--include",
        choices=[type.value for type in MediaType],
        help="Filter: Include the specified type for processing." 
    )

    parser.add_argument(
        "--exclude",
        choices=[type.value for type in MediaType],
        help="Filter: Exclude the specified type from processing." 
    )

    # Other options
    parser.add_argument(
        "--dry-run",
        action="store_true", 
        help="Perform a trial run without making any changes."
    )

    # Verbose output
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output."
    )

    args = parser.parse_args()

    args.source = args.source.resolve() # eg. from "./media" to "C:/media"
    args.backup = args.backup.resolve()

    # validation only one filter mode
    filter_modes = [args.only, args.include, args.exclude]
    if sum(mode is not None for mode in filter_modes) > 1: 
        parser.error("Only one of --only, --include, or --exclude can be specified.")

    return args

def validate_path(path):
    if not path.exists():
        raise ValueError(f"error: {path} does not exist")
    if not path.is_dir():
        raise ValueError(f"error: {path} not a directory")
    logger.info(f"Validated path: {path}")

def print_free_space(path):
    free_space = shutil.disk_usage(path.drive).free / (1024 ** 3)
    logger.info(f"Free space {path.drive}: {free_space:.2f} GB")

def check_free_space(backup_path, media_files):
    logger.info(f"Checking free space on {backup_path} ...")

    if not media_files:
        raise ValueError("No media files to process.")

    required_size = sum(m.size_bytes for m in media_files)
    backup_free = shutil.disk_usage(backup_path.drive).free
    if required_size > backup_free:
        raise ValueError("Not enough free space on backup drive")
    
    logger.info("Sufficient free space on backup drive")

def backup_files(media_files, path, dry_run=False):
    logger.info(f"Backing up media files to {path}...")

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
            if dry_run:
                logger.debug(f"[dry-run] would copy {media.path} to {dest_dir}")
            else:
                copy_file(media, dest_dir)
            seen_hashes.add(media.hash)
            copied += 1
        except Exception as e:
            logger.error("error backing up", media.path, ":", e)

    logger.info(f"Backup complete. copied=[{copied}], skipped=[{skipped}]") #todo: size skipped

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

    logger.info("Scan summary:")
    logger.info(f"Total media files : {total_files}")
    logger.info(f"Images           : {image_files}")
    logger.info(f"Videos           : {video_files}")

    logger.info(f"Size summary:")
    logger.info(f"Total size :  {bytes_to_gb(total_size):.2f} GB")
    logger.info(f"Images size :  {bytes_to_gb(image_size):.2f} GB")
    logger.info(f"Videos size :  {bytes_to_gb(video_size):.2f} GB")

def apply_media_folders_filter(media_files, args):
    if args.only:
        flt_only_type = MediaType(args.only) 
        return [m for m in media_files if m.type == flt_only_type]

    if args.include:
        flt_include_type = MediaType(args.include)
        return [m for m in media_files if m.type == flt_include_type]

    if args.exclude:
        flt_exclude_type = MediaType(args.exclude)
        return [m for m in media_files if m.type != flt_exclude_type]

    return media_files

def classify_media_files(media_files):

    return

def main():
    args = parse_args()
    logger_init(args.verbose)


    logger.info("iBackup starting...")

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

    media_files = apply_media_folders_filter(media_files, args)

    # check free space
    check_free_space(args.backup, media_files)
    # compute hashes
    compute_hashes(media_files)

    # find duplicates
    duplicates = find_duplicates(media_files)    

    # backup files
    backup_files(media_files, args.backup, args.dry_run)

    # calculate stats
    print_stats(media_files)


if __name__ == "__main__":
    start = time.perf_counter()
    main()
    elapsed = time.perf_counter() - start
    logger.info(f"Elapsed time: {elapsed:.2f} seconds")