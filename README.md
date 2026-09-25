# iBackup

Back up photos from an iPhone (or from old backup folders) onto an SSD, sorted
into `year / month / location`, without duplicates, with a SQLite index that
remembers everything.

```
E:\Backup\Photos\
  2025\
    01_ianuarie\
      Barcelona\
        IMG_0001.HEIC
        IMG_0001.MOV      <- Live Photo, kept next to its still
      Fara_locatie\
  2024\
    08_august\
      Bucuresti\
  Screenshots\
    2025\01_ianuarie\
  .ibackup\
    index.db              <- the index; it travels with the SSD
    config.toml
    logs\
```

Month folders are Romanian by default (`language = "ro"`); pass `--language en`
or edit the config for English ones.

## Requirements

- Python 3.11 or newer. **No required dependencies** - the core is stdlib only.
- For the iPhone, one of:
  - `pip install pymobiledevice3` — recommended (AFC), plus the **Apple Devices**
    app from the Microsoft Store (or iTunes) for the USB driver;
  - `pip install pywin32` — the MTP route, which works with the driver Windows
    already ships.

## Quick start

```bash
python -m ibackup --dest E:/Backup/Photos init
python -m ibackup --dest E:/Backup/Photos devices
python -m ibackup --dest E:/Backup/Photos scan --source D:/OldPhotos
python -m ibackup --dest E:/Backup/Photos import --source D:/OldPhotos
python -m ibackup --dest E:/Backup/Photos import --source iphone
python -m ibackup --dest E:/Backup/Photos status
```

After `init` the path is stored in `config.toml`, so from elsewhere you can pass
`--config E:/Backup/Photos/.ibackup/config.toml` instead of `--dest`.

## Commands

| Command | What it does |
|---|---|
| `init` | Create the structure, `config.toml` and an empty index. |
| `scan --source X` | Dry run: say what would be copied, write nothing. |
| `import --source X` | Copy for real. `X` = a folder, `iphone`, `iphone:afc`, `iphone:mtp`. |
| `devices` | Which ways of reaching the phone are available right now. |
| `status` | What the backup holds: by year, by location, how much space. |
| `verify [--deep]` | Check the files on the SSD against the index. |
| `relocate` | Run the location guessing on its own. |
| `reorganize` | Re-apply the layout rules to the whole backup after a config change. |
| `reindex` | Rebuild the index from the existing files (if `index.db` is lost). |
| `geonames --download` | Install the full place dataset for geocoding. |

Useful `import` options: `--dry-run`, `--limit N`, `--verbose`, `--no-infer`,
`--strict-dedup`, `--no-cache`, `--language en`, `--radius-km 10`,
`--only`/`--include`/`--exclude` followed by any of
`photo screenshot video sidecar`.

## How it works

**1. The fingerprint.** For every file we compute `size + MD5(first 64 KB + last
64 KB)`. It is cheap, and over AFC it can be computed by reading only 128 KB from
the phone, so a duplicate is recognised without downloading the photo.

**2. Deduplication.** The fingerprint is looked up in the index. When the file is
already on this machine (a folder source), a match is confirmed with a full MD5
before we call it a duplicate, so a fingerprint collision can never make us drop
a photo. For the iPhone that confirmation would mean downloading the file just to
learn we already have it, so by default size + partial MD5 is trusted there;
`--strict-dedup` turns confirmation on for the phone too.

**3. The source cache.** What we learned about a source file - its fingerprint
and the decision we reached - is remembered against its path, size and mtime.
Re-running the same folder then costs a directory walk and one `stat` per file,
with no reads at all. `--no-cache` forces a re-read.

**4. The date.** In order of trust: EXIF `DateTimeOriginal` → `DateTimeDigitized`
→ EXIF `DateTime` → `GPSDateStamp` → for video, `com.apple.quicktime.creationdate`
or the `©day` atom (both carry a UTC offset, so they give the true local time) →
`mvhd` → a date in the filename (`IMG_20190712_183042`,
`Screenshot 2023-05-01 at 12.33.44`, `IMG-20190712-WA0001`) → the file's
modification time. Whichever source won is stored in the index, so you can tell
how confident any photo's placement is.

**5. The location.** GPS coordinates from EXIF (or ISO 6709 from video) are
rounded to about a kilometre and looked up in a city database. Among the cities
within the radius (25 km by default) the **largest** one wins - which is why a
photo taken in Badalona lands in `Barcelona` instead of a folder of its own. With
no city in range the folder becomes `GPS_44.4N_26.1E`. With no GPS at all:
`Fara_locatie`.

**6. Guessed locations.** At the end, photos left in `Fara_locatie` take the
location of the nearest photo in time that does have GPS (±3 hours by default)
and are moved there. Only files currently in `Fara_locatie` are moved, so this
can never disturb a placement based on real coordinates. Turn it off with
`--no-infer`.

**7. Screenshots** are filed by kind rather than place - they carry no GPS and
would otherwise fill the no-location folder. A file is a screenshot if its name
or folder says so, if its EXIF `Software` tag says so, or if it is a PNG with no
EXIF date, no GPS and no camera name. `--verbose` prints which rule fired.

**8. Writing.** A temporary `.part` file → verification (size + fingerprint) →
atomic rename. An interruption leaves no half-written files, and the original's
modification time is preserved. Copying stops with a clear message before the
drive fills up (`safety.min_free_gb`, default 1 GB).

## What it does not do

- **It never deletes anything**, on the phone or in the source folders.
- It does not modify files - they are copied byte for byte.
- It sends nothing over the internet. The one exception is
  `geonames --download`, which you start yourself.

## Configuration

`E:\Backup\Photos\.ibackup\config.toml`:

```toml
dest = "E:/Backup/Photos"

[organize]
language = "ro"                 # month folder names: ro or en
unknown_place = "Fara_locatie"
video_subdir = ""               # e.g. "Video" to keep videos separate
screenshot_dir = "Screenshots"  # own tree; "./Screenshots" = inside the month;
                                # "" = file screenshots like ordinary photos
detect_screenshots = true
rename_with_timestamp = false   # true => 20250114_183042_IMG_0001.heic

[dedup]
partial_chunk_kb = 64
confirm_with_full_md5 = true
strict_remote_dedup = false

[geo]
radius_km = 25.0
prefer = "population"           # or "nearest"
dataset = ""
cluster_decimals = 2

[geo.aliases]
Bucuresti = "Home"              # rename a location folder

[infer]
enabled = true
window_hours = 3.0

[include]
videos = true
sidecars = true
source_cache = true

[safety]
verify_after_copy = true
min_free_gb = 1.0
```

Layout settings can be changed on a backup that already exists: edit the config
and run `reorganize`. (Filenames are left alone, so `rename_with_timestamp`
applies to newly imported files only.)

## iPhone: read this first

- **Unlock the phone** and accept *Trust This Computer* on first connection.
- On **MTP**, set `Settings > Photos > Transfer to Mac or PC` to **Keep
  Originals**. On *Automatic* the iPhone converts HEIC to JPEG while copying, so
  you get a different file from the original - a different fingerprint too, which
  means it will be copied again on every run.
- We read `/DCIM`, the camera roll. Photos that live only in iCloud and are not
  downloaded to the phone will not appear; for those, set
  `Settings > Photos > Download and Keep Originals` and wait for the sync.
- AFC is considerably faster and checks duplicates without downloading. If
  `devices` shows `NO` for both, install `pymobiledevice3` and the *Apple
  Devices* app.
- `--exclude screenshot` still fetches each photo before it can be classified,
  since a screenshot cannot be recognised from its name. Over USB that costs
  real time.

## Geocoding: built-in list vs. GeoNames

By default the list in `ibackup/geocode.py` is used - 211 major cities with
approximate centres. It covers cities, not villages. For real resolution:

```bash
python -m ibackup --dest E:/Backup/Photos geonames --download
```

That fetches `cities5000.zip` (~2 MB) from geonames.org and converts it into a
local TSV - roughly 50,000 places, offline, permanently. To download it yourself
instead, use `geonames --install cities5000.zip`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

115 tests, no dependencies. The test files (JPEG with EXIF, PNG, HEIC, MOV) are
built byte by byte in `tests/synth.py`, so the tests exercise exactly our parsers
without keeping real photos in the repo.

## What changed in v0.2

v0.1 was a working proof of concept that stopped short of the hard parts. v0.2
keeps what it got right and finishes the rest.

Kept from v0.1: screenshot detection, the free-space check before copying, the
`--only`/`--include`/`--exclude` filters, and the idea of caching hashes against
size and mtime.

Added: dates from EXIF and QuickTime instead of `mtime` alone — which is the
important one, since `mtime` on an old backup folder is when the file was last
copied, not when the photo was taken; reverse geocoding into city folders;
reading the iPhone directly over AFC or MTP; Live Photo pairing; collision-safe
naming; atomic writes with verification; an index of the backup rather than of
the sources; `verify`, `reindex`, `relocate` and `reorganize`; and a test suite.

Fixed along the way: two photos with the same name in the same month used to
overwrite each other silently; the "already backed up" check looked in the backup
root instead of the dated folder, so it never fired and every run re-copied
everything; `--dry-run` created the whole folder tree before deciding not to
write; and four logging calls used `print`-style varargs or a missing `f` prefix,
in the error handlers.

Dropped: blake3, which was a dependency for no gain here — full hashing only runs
to confirm a local duplicate, and `verify --deep` uses the partial fingerprint;
and the screenshot resolution heuristic, which flagged anything at or below
1792×828 and so misfiled every resized photo.
