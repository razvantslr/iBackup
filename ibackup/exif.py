"""A stdlib-only EXIF reader (TIFF/JPEG/PNG).

Pillow is avoided so the tool has no binary wheels to install: all we need from
EXIF is the date and the GPS coordinates, and TIFF is simple enough to read
directly. The parser is defensive — a damaged file returns empty values instead
of raising, because one corrupt JPEG must not stop a backup.
"""
from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

TAG_SOFTWARE = 0x0131
TAG_DATETIME = 0x0132
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_OFFSET_TIME_ORIGINAL = 0x9011

GPS_LAT_REF = 0x0001
GPS_LAT = 0x0002
GPS_LON_REF = 0x0003
GPS_LON = 0x0004
GPS_TIMESTAMP = 0x0007
GPS_DATESTAMP = 0x001D

_TYPE_FMT = {
    1: ("B", 1),    # BYTE
    2: ("s", 1),    # ASCII
    3: ("H", 2),    # SHORT
    4: ("I", 4),    # LONG
    5: ("II", 8),   # RATIONAL
    6: ("b", 1),    # SBYTE
    7: ("s", 1),    # UNDEFINED
    8: ("h", 2),    # SSHORT
    9: ("i", 4),    # SLONG
    10: ("ii", 8),  # SRATIONAL
    11: ("f", 4),   # FLOAT
    12: ("d", 8),   # DOUBLE
}

MAX_IFD_ENTRIES = 512


def _read_value(buf, order, typ, count, raw, base):
    fmt, size = _TYPE_FMT[typ]
    total = size * count
    if total > 4:
        (offset,) = struct.unpack(order + "I", raw)
        start = base + offset
        if start < 0 or start + total > len(buf):
            return None
        data = buf[start:start + total]
    else:
        data = raw[:total]
    if len(data) < total:
        return None

    if typ in (2, 7):
        if typ == 2:
            return data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        return data

    if typ in (5, 10):
        values = []
        unit = "II" if typ == 5 else "ii"
        for i in range(count):
            num, den = struct.unpack(order + unit, data[i * 8:(i + 1) * 8])
            values.append((num, den))
        return values[0] if count == 1 else values

    values = struct.unpack(order + fmt * count, data)
    return values[0] if count == 1 else list(values)


def parse_ifd(buf, offset, order, base=0):
    """Read one TIFF IFD and return it as {tag: value}."""
    tags = {}
    if offset < 0 or offset + 2 > len(buf):
        return tags
    (count,) = struct.unpack_from(order + "H", buf, offset)
    count = min(count, MAX_IFD_ENTRIES)
    pos = offset + 2
    for _ in range(count):
        if pos + 12 > len(buf):
            break
        tag, typ, cnt = struct.unpack_from(order + "HHI", buf, pos)
        raw = buf[pos + 8:pos + 12]
        pos += 12
        if typ not in _TYPE_FMT or cnt > (1 << 20):
            continue
        try:
            value = _read_value(buf, order, typ, cnt, raw, base)
        except Exception:
            value = None
        if value is not None:
            tags[tag] = value
    return tags


def parse_tiff(buf):
    """Split a TIFF/EXIF block into the three IFDs we care about."""
    result = {"ifd0": {}, "exif": {}, "gps": {}}
    if len(buf) < 8:
        return result
    if buf[:2] == b"II":
        order = "<"
    elif buf[:2] == b"MM":
        order = ">"
    else:
        return result
    magic, first_ifd = struct.unpack_from(order + "HI", buf, 2)
    if magic != 42:
        return result
    ifd0 = parse_ifd(buf, first_ifd, order)
    result["ifd0"] = ifd0
    exif_off = ifd0.get(TAG_EXIF_IFD)
    if isinstance(exif_off, int):
        result["exif"] = parse_ifd(buf, exif_off, order)
    gps_off = ifd0.get(TAG_GPS_IFD)
    if isinstance(gps_off, int):
        result["gps"] = parse_ifd(buf, gps_off, order)
    return result


def _parse_exif_datetime(text):
    if not isinstance(text, str):
        return None
    text = text.strip().replace("/", ":")
    if not text or text.startswith("0000"):
        return None
    for fmt, width in (("%Y:%m:%d %H:%M:%S", 19), ("%Y:%m:%d %H:%M", 16),
                       ("%Y-%m-%d %H:%M:%S", 19), ("%Y:%m:%d", 10)):
        try:
            return datetime.strptime(text[:width], fmt)
        except ValueError:
            continue
    return None


def exif_datetime(tags):
    """The photo's local timestamp plus a label saying where it came from."""
    exif = tags.get("exif", {})
    ifd0 = tags.get("ifd0", {})
    for source, value in (
        ("exif:DateTimeOriginal", exif.get(TAG_DATETIME_ORIGINAL)),
        ("exif:DateTimeDigitized", exif.get(TAG_DATETIME_DIGITIZED)),
        ("exif:DateTime", ifd0.get(TAG_DATETIME)),
    ):
        dt = _parse_exif_datetime(value)
        if dt is not None:
            return dt, source
    return None, ""


def _rational(value):
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        return float(num) / float(den) if den else None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def exif_gps(tags):
    """Coordinates as (lat, lon) in decimal degrees, or None."""
    gps = tags.get("gps", {})
    coords = []
    for vals, ref in ((gps.get(GPS_LAT), gps.get(GPS_LAT_REF)),
                      (gps.get(GPS_LON), gps.get(GPS_LON_REF))):
        if not isinstance(vals, list) or len(vals) < 2:
            return None
        parts = [_rational(v) for v in vals[:3]]
        if parts[0] is None:
            return None
        deg = parts[0] + (parts[1] or 0.0) / 60.0
        if len(parts) > 2 and parts[2]:
            deg += parts[2] / 3600.0
        if isinstance(ref, str) and ref.strip().upper()[:1] in ("S", "W"):
            deg = -deg
        coords.append(deg)
    lat, lon = coords
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    # Some cameras write 0/0 when the GPS fix failed; that is not the Gulf of Guinea.
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None
    return lat, lon


def exif_gps_datetime(tags):
    """The UTC timestamp from the GPS tags - a safety net when EXIF has none."""
    gps = tags.get("gps", {})
    stamp = gps.get(GPS_DATESTAMP)
    if not isinstance(stamp, str):
        return None
    try:
        date = datetime.strptime(stamp.strip()[:10], "%Y:%m:%d")
    except ValueError:
        return None
    parts = gps.get(GPS_TIMESTAMP)
    if isinstance(parts, list) and len(parts) == 3:
        h = _rational(parts[0]) or 0.0
        m = _rational(parts[1]) or 0.0
        s = _rational(parts[2]) or 0.0
        date += timedelta(hours=h, minutes=m, seconds=s)
    return date.replace(tzinfo=timezone.utc)


def exif_camera(tags):
    ifd0 = tags.get("ifd0", {})
    make = ifd0.get(TAG_MAKE)
    model = ifd0.get(TAG_MODEL)
    return (make if isinstance(make, str) else None,
            model if isinstance(model, str) else None)


def exif_software(tags):
    """The Software tag - screenshots often name the tool that produced them."""
    value = tags.get("ifd0", {}).get(TAG_SOFTWARE)
    return value if isinstance(value, str) else None


def exif_from_jpeg(data):
    """Pull the TIFF block out of a JPEG's APP1 segment."""
    if data[:2] != b"\xff\xd8":
        return None
    pos, n = 2, len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xDA, 0xD9):
            return None
        (seg_len,) = struct.unpack_from(">H", data, pos + 2)
        if seg_len < 2:
            return None
        seg_start, seg_end = pos + 4, pos + 2 + seg_len
        if marker == 0xE1 and data[seg_start:seg_start + 6] == b"Exif\x00\x00":
            return data[seg_start + 6:seg_end]
        pos = seg_end
    return None


def exif_from_png(data):
    """Modern PNGs, including iOS screenshots, carry an eXIf chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, n = 8, len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack_from(">I", data, pos)
        ctype = data[pos + 4:pos + 8]
        start = pos + 8
        if ctype == b"eXIf":
            return data[start:start + length]
        if ctype in (b"IDAT", b"IEND"):
            return None
        pos = start + length + 4
    return None
