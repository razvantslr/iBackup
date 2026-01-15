import logging
from collections import defaultdict

def find_duplicates(media_files):
    logging.info("Finding duplicate media files ...")
    
    duplicates = defaultdict(list)
    for media in media_files:
        if media.hash is None:
            continue
        duplicates[media.hash].append(media)

    duplicate_groups = {
        h: files for h, files in duplicates.items()
        if len(files) > 1
    }

    logging.info("Duplicate files:")
    if not duplicate_groups:
        logging.info("No duplicates found.")
    else:
        for h, files in duplicate_groups.items():
            logging.info(f"\nHash: {h}")
            for f in files:
                logging.info(f" {f.path}")
    
    return duplicate_groups