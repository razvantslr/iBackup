import logging
from collections import defaultdict

logger = logging.getLogger("iBackup")

def find_duplicates(media_files):
    logger.info("Finding duplicate media files ...")
    
    duplicates = defaultdict(list)
    for media in media_files:
        if media.hash is None:
            continue
        duplicates[media.hash].append(media)

    duplicate_groups = {
        h: files for h, files in duplicates.items()
        if len(files) > 1
    }

    logger.info("Duplicate files:")
    if not duplicate_groups:
        logger.info("No duplicates found.")
    else:
        for h, files in duplicate_groups.items():
            logger.info(f"Hash: {h}")
            for f in files:
                logger.info(f" {f.path}")
    
    return duplicate_groups