import logging
from blake3 import blake3
from pathlib import Path

from db import init_db, get_indexed_file, upsert_file


def compute_blake3_hash(file_path: str, chunk_size=1024 * 1024) -> str:
    hasher = blake3()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

def compute_hashes(media_files):
    logging.info("Computing hashes for media files ...")
    
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
            logging.error("error computhing hash for", media.path, ":", e)
            continue

        upsert_file(conn, media)

        if index % 10 == 0 or index == total:
            logging.info(f"[{index}/{total}] hashed")
    
    logging.debug("\nHash sanity check:")
    for media in media_files[:5]:
        logging.debug(media.hash, media.path)
