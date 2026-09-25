"""Configuration: defaults, the TOML file, and command-line overrides."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

INTERNAL_DIR = ".ibackup"
DB_NAME = "index.db"
CONFIG_NAME = "config.toml"


@dataclass
class Config:
    # Where the backup lives. Everything else is relative to it.
    dest: Path

    # Layout
    language: str = "ro"
    unknown_place: str = "Fara_locatie"
    video_subdir: str = ""          # empty: videos sit next to photos
    # "Screenshots" = its own tree at the root; "./Screenshots" = inside the
    # month folder; "" = no special handling, screenshots are filed as photos.
    screenshot_dir: str = "Screenshots"
    detect_screenshots: bool = True
    rename_with_timestamp: bool = False

    # Duplicates
    partial_chunk_kb: int = 64
    confirm_with_full_md5: bool = True
    # Confirming needs the whole file. For an iPhone that would mean downloading
    # a photo just to learn we already have it, so by default we trust size plus
    # the partial MD5 for remote sources.
    strict_remote_dedup: bool = False

    # Geolocation
    geo_radius_km: float = 25.0
    geo_prefer: str = "population"  # population | nearest
    geo_dataset: str = ""
    geo_cluster_decimals: int = 2
    geo_aliases: dict[str, str] = field(default_factory=dict)

    # Guessing a location from photos taken close in time
    infer_enabled: bool = True
    infer_window_hours: float = 3.0

    # What we take
    include_videos: bool = True
    include_sidecars: bool = True
    skip_dirs: list[str] = field(default_factory=lambda: [
        INTERNAL_DIR, "$RECYCLE.BIN", "System Volume Information", ".git",
        "@eaDir", ".Trash", ".Trashes",
    ])

    # Safety
    verify_after_copy: bool = True
    # Refuse to keep copying once the backup drive drops below this.
    min_free_gb: float = 1.0

    @property
    def internal_dir(self) -> Path:
        return self.dest / INTERNAL_DIR

    @property
    def db_path(self) -> Path:
        return self.internal_dir / DB_NAME

    @property
    def config_path(self) -> Path:
        return self.internal_dir / CONFIG_NAME

    @property
    def log_dir(self) -> Path:
        return self.internal_dir / "logs"

    @property
    def staging_dir(self) -> Path:
        return self.internal_dir / "staging"

    @property
    def partial_chunk(self) -> int:
        return max(4, self.partial_chunk_kb) * 1024


_SECTION_KEYS = {
    "organize": {
        "language": "language",
        "unknown_place": "unknown_place",
        "video_subdir": "video_subdir",
        "screenshot_dir": "screenshot_dir",
        "detect_screenshots": "detect_screenshots",
        "rename_with_timestamp": "rename_with_timestamp",
    },
    "dedup": {
        "partial_chunk_kb": "partial_chunk_kb",
        "confirm_with_full_md5": "confirm_with_full_md5",
        "strict_remote_dedup": "strict_remote_dedup",
    },
    "geo": {
        "radius_km": "geo_radius_km",
        "prefer": "geo_prefer",
        "dataset": "geo_dataset",
        "cluster_decimals": "geo_cluster_decimals",
        "aliases": "geo_aliases",
    },
    "infer": {
        "enabled": "infer_enabled",
        "window_hours": "infer_window_hours",
    },
    "include": {
        "videos": "include_videos",
        "sidecars": "include_sidecars",
        "skip_dirs": "skip_dirs",
    },
    "safety": {
        "verify_after_copy": "verify_after_copy",
        "min_free_gb": "min_free_gb",
    },
}


def _apply_table(cfg: Config, data: dict) -> Config:
    updates: dict = {}
    for section, mapping in _SECTION_KEYS.items():
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for key, attr in mapping.items():
            if key in table:
                updates[attr] = table[key]
    if "dest" in data:
        updates["dest"] = Path(str(data["dest"]))
    return replace(cfg, **updates) if updates else cfg


def load_config(dest: Path | None = None, config_file: Path | None = None) -> Config:
    """Defaults < TOML file < the --dest argument."""
    data: dict = {}
    if config_file is not None:
        with open(config_file, "rb") as fh:
            data = tomllib.load(fh)
    elif dest is not None:
        candidate = Path(dest) / INTERNAL_DIR / CONFIG_NAME
        if candidate.is_file():
            with candidate.open("rb") as fh:
                data = tomllib.load(fh)

    base_dest = Path(dest) if dest is not None else None
    if base_dest is None and "dest" in data:
        base_dest = Path(str(data["dest"]))
    if base_dest is None:
        raise ValueError(
            "I do not know where the backup lives. Pass --dest E:/Backup/Photos "
            "or set dest = \"E:/Backup/Photos\" in config.toml."
        )

    cfg = Config(dest=Path(base_dest))
    cfg = _apply_table(cfg, data)
    if dest is not None:  # an explicit argument always wins
        cfg = replace(cfg, dest=Path(dest))
    return cfg


def write_default_config(cfg: Config) -> Path:
    """Write a commented config.toml next to the index, unless one exists."""
    path = cfg.config_path
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    dest_text = str(cfg.dest).replace("\\", "/")
    path.write_text(
        f'''# iBackup configuration. Every option can also be given on the command line.
dest = "{dest_text}"

[organize]
language = "ro"               # language of the month folder names: ro or en
unknown_place = "Fara_locatie"
video_subdir = ""             # e.g. "Video" to keep videos separate
screenshot_dir = "Screenshots"  # own tree; "./Screenshots" = inside the month;
                                # "" = file screenshots like ordinary photos
detect_screenshots = true
rename_with_timestamp = false # true => 20250114_183042_IMG_0001.heic

[dedup]
partial_chunk_kb = 64         # how much we read from the head and tail
confirm_with_full_md5 = true  # confirm duplicates with a full MD5
strict_remote_dedup = false   # true = confirm for the iPhone too, which means
                              # downloading every duplicate to check it

[geo]
radius_km = 25.0              # how far away a city may be
prefer = "population"         # population = the big city nearby; nearest = closest
dataset = ""                  # path to cities5000.txt, if installed
cluster_decimals = 2          # coordinate rounding before lookup (~1 km)

[geo.aliases]
# Bucuresti = "Acasa"

[infer]
enabled = true                # guess a location from photos close in time
window_hours = 3.0

[include]
videos = true
sidecars = true

[safety]
verify_after_copy = true
min_free_gb = 1.0             # stop before the backup drive fills up
''',
        encoding="utf-8",
    )
    return path
