"""Configurarea tool-ului: valori implicite, fisier TOML, suprascrieri din CLI."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

INTERNAL_DIR = ".ibackup"
DB_NAME = "index.db"
CONFIG_NAME = "config.toml"


@dataclass
class Config:
    # Unde traieste backup-ul. Tot restul se aseaza relativ la el.
    dest: Path

    # Organizare
    language: str = "ro"
    unknown_place: str = "Fara_locatie"
    video_subdir: str = ""          # gol = filmele stau langa poze
    rename_with_timestamp: bool = False

    # Duplicate
    partial_chunk_kb: int = 64
    confirm_with_full_md5: bool = True
    # Confirmarea cere continutul integral. Pentru iPhone asta ar insemna sa
    # descarcam fisierul tocmai ca sa aflam ca il avem deja, asa ca implicit
    # avem incredere in dimensiune + MD5 partial la sursele de retea.
    strict_remote_dedup: bool = False

    # Geolocatie
    geo_radius_km: float = 25.0
    geo_prefer: str = "population"  # population | nearest
    geo_dataset: str = ""
    geo_cluster_decimals: int = 2
    geo_aliases: dict[str, str] = field(default_factory=dict)

    # Deducerea locatiei din pozele vecine in timp
    infer_enabled: bool = True
    infer_window_hours: float = 3.0

    # Ce luam
    include_videos: bool = True
    include_sidecars: bool = True
    skip_dirs: list[str] = field(default_factory=lambda: [
        INTERNAL_DIR, "$RECYCLE.BIN", "System Volume Information", ".git",
        "@eaDir", ".Trash", ".Trashes",
    ])

    # Siguranta
    verify_after_copy: bool = True

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
    """Incarca configul. Ordinea: valori implicite < fisier TOML < argument --dest."""
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
            "Nu stiu unde e backup-ul. Foloseste --dest E:/Backup/Poze sau "
            "pune dest = \"E:/Backup/Poze\" in config.toml."
        )

    cfg = Config(dest=Path(base_dest))
    cfg = _apply_table(cfg, data)
    if dest is not None:  # argumentul explicit are ultimul cuvant
        cfg = replace(cfg, dest=Path(dest))
    return cfg


def write_default_config(cfg: Config) -> Path:
    """Scrie un config.toml comentat langa baza de date, daca nu exista deja."""
    path = cfg.config_path
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    dest_text = str(cfg.dest).replace("\\", "/")
    path.write_text(
        f'''# Configurarea backup-ului de poze. Orice optiune poate fi data si din CLI.
dest = "{dest_text}"

[organize]
language = "ro"              # numele lunilor: ro sau en
unknown_place = "Fara_locatie"
video_subdir = ""            # ex. "Video" daca vrei filmele separat
rename_with_timestamp = false

[dedup]
partial_chunk_kb = 64        # cat citim din capul si coada fisierului
confirm_with_full_md5 = true # confirma duplicatele cu MD5 complet
strict_remote_dedup = false  # true = confirma cu MD5 complet si pentru iPhone
                             # (mai sigur, dar descarca fiecare duplicat)

[geo]
radius_km = 25.0             # in ce raza cautam un oras
prefer = "population"        # population = orasul mare din zona; nearest = cel mai apropiat
dataset = ""                 # calea catre cities5000.txt, daca l-ai instalat
cluster_decimals = 2         # rotunjirea coordonatelor inainte de cautare (~1 km)

[geo.aliases]
# Bucuresti = "Acasa"

[infer]
enabled = true               # deduce locatia din pozele apropiate in timp
window_hours = 3.0

[include]
videos = true
sidecars = true

[safety]
verify_after_copy = true
''',
        encoding="utf-8",
    )
    return path
