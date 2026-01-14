from dataclasses import dataclass
from typing import Optional


# constants.py
SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.heic'}
SUPPORTED_VIDEO_EXT = {'.mp4', '.mov'}
SUPPORTED_MEDIA_EXT = SUPPORTED_IMAGE_EXT | SUPPORTED_VIDEO_EXT


@dataclass
class MediaFile:
    path: str
    extension: str
    media_type: str # 'image' | 'video' 
    size_bytes: int
    hash: Optional[str] = None


def bytes_to_gb(num_bytes: int) -> float:
    return num_bytes / (1024 ** 3)

def scan_for_media_files(path):  
    media_files = [] # todo: move

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

        print(f"\nCollected {len(media_files)} media files\n")    
        print("\nFiles sanity check:")
        for media in media_files[:5]:
            print(media)
