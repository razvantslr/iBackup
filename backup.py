import shutil
from pathlib import Path
from datetime import datetime


def get_file_date(path: str):
    stat = Path(path).stat()
    return datetime.fromtimestamp(stat.st_mtime)

def build_backup_path(base_dest, media):
    date = get_file_date(media.path)
    year = date.strftime("%Y")      # 2024
    month = date.strftime("%m_%B")  # 07_July
    type = media.type               # image / video / screenshot

    dest_dir = Path(base_dest) / year / month / type
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir

# todo: move
def file_exists(dest_dir, media):
    #dest_dir = Path(dest_dir)
    return (dest_dir / Path(media.path).name).exists()

def copy_file(media, dest_dir):
    src = Path(media.path)
    dst = dest_dir / src.name
    shutil.copy2(src,dst)

def backup_files(media_files, path):
    # backup files
    backup_path = path
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
