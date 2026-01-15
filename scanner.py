import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS

# constants.py
SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.heic'}
SUPPORTED_VIDEO_EXT = {'.mp4', '.mov'}
SUPPORTED_MEDIA_EXT = SUPPORTED_IMAGE_EXT | SUPPORTED_VIDEO_EXT

class MediaType(Enum):
    IMAGE = 'image'
    VIDEO = 'video'
    SCREENSHOT = 'screenshot'

@dataclass
class MediaFile:
    path: str
    extension: str
    type: MediaType
    size_bytes: int
    hash: Optional[str] = None


def bytes_to_gb(num_bytes: int) -> float:
    return num_bytes / (1024 ** 3)

def filename_contains_screenshot_keyword(path):
    filename = Path(path).name.lower()
    screenshot_keywords = ['screenshot', 'screen_shot', 'screen-shot']
    return any(keyword in filename for keyword in screenshot_keywords) 

def path_contains_screenshot_keyword(path):
    path_parts = Path(path).parts
    screenshot_keywords = ['screenshot', 'screen_shot', 'screen-shot']
    for part in path_parts:
        part_lower = part.lower()
        if any(keyword in part_lower for keyword in screenshot_keywords):
            return True
    return False

def exif_screenshot_check(path):
    try:
        image = Image.open(path)
        exif = image._getexif()
        if not exif:
            return False
        data = {TAGS.get(tag): value for tag, value in exif.items()}
        software = data.get('Software', '').lower()
        make = data.get('Make', '').lower()
        model = data.get('Model', '').lower()
        if 'screenshot'in software:
            return True
        if not make and not model:
            return True
    except Exception:
        pass

    return False

def resolution_screenshot_check(path) -> bool:
    try:
        image = Image.open(path)
        width, height = image.size
        
        ss_max_with = 1792
        ss_max_height = 828

        if (width <= ss_max_with and height <= ss_max_height) \
        or (height <= ss_max_with and width <= ss_max_height):
            return True
        
    except Exception:
        pass

    return False
 
def is_screenshot(path) -> bool:
    if filename_contains_screenshot_keyword(path):
        return True
    if path_contains_screenshot_keyword(path):
        return True
    if exif_screenshot_check(path):
        return True
    if resolution_screenshot_check(path):
        return True
    return False

def scan_for_media_files(path):  
    media_files = []

    for item in path.rglob('*'):
        # skip non-files
        if not item.is_file():
            continue
        # setting media type
        ext = item.suffix.lower()
        if ext in SUPPORTED_IMAGE_EXT:
            type = MediaType.IMAGE
        elif ext in SUPPORTED_VIDEO_EXT:
            type = MediaType.VIDEO
        else:
            continue
        # screenshot detection
        if type == MediaType.IMAGE and is_screenshot(item):
                type = MediaType.SCREENSHOT
        # create MediaFile object and add to list
        media = MediaFile(
            path=str(item),
            extension=ext,
            type=type,
            size_bytes=item.stat().st_size,
        )
        media_files.append(media)

    # logging
    logging.info(f"Collected {len(media_files)} media files\n")    
    logging.debug("\nFiles sanity check:")
    for media in media_files[:5]:
        logging.debug(media)

    return media_files