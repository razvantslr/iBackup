"""Reading ISO-BMFF containers: HEIC/HEIF and QuickTime/MP4.

An iPhone's HEIC and MOV files use the same box format, so one parser covers
both photos and videos. From HEIC we extract the EXIF block (which is then
handed to exif.py); from MOV/MP4 we take the creation date and the ISO 6709
coordinates Apple writes.
"""
from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone

# QuickTime counts seconds from 1 January 1904, UTC.
QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

MAX_BOXES_PER_LEVEL = 4096
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm",
               b"hevs", b"mif1", b"msf1", b"avif", b"avis"}
QT_BRANDS = {b"qt  ", b"isom", b"mp42", b"mp41", b"M4V ", b"M4A ", b"avc1",
             b"iso2", b"iso4", b"iso5", b"iso6", b"mmp4", b"3gp4", b"3gp5"}


class Box:
    __slots__ = ("type", "start", "end")

    def __init__(self, type_: bytes, start: int, end: int):
        self.type = type_
        self.start = start  # first payload byte
        self.end = end      # exclusive

    @property
    def size(self) -> int:
        return self.end - self.start

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Box({self.type!r}, {self.start}-{self.end})"


def iter_boxes(fh, start: int, end: int):
    """Walk the boxes between two file offsets."""
    pos = start
    seen = 0
    while pos + 8 <= end and seen < MAX_BOXES_PER_LEVEL:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size, btype = struct.unpack(">I4s", header)
        body = pos + 8
        if size == 1:
            raw = fh.read(8)
            if len(raw) < 8:
                return
            (size,) = struct.unpack(">Q", raw)
            body = pos + 16
        elif size == 0:
            size = end - pos
        box_end = pos + size
        if size < 8 or box_end > end or body > box_end:
            return
        yield Box(btype, body, box_end)
        pos = box_end
        seen += 1


def find_box(fh, start: int, end: int, wanted: bytes):
    for box in iter_boxes(fh, start, end):
        if box.type == wanted:
            return box
    return None


def read_ftyp(fh, file_size: int) -> bytes | None:
    for box in iter_boxes(fh, 0, file_size):
        if box.type == b"ftyp":
            fh.seek(box.start)
            return fh.read(4)
        if box.type not in (b"free", b"skip", b"wide"):
            break
    return None


def container_kind(fh, file_size: int) -> str | None:
    """'heif', 'quicktime' or None, based on the brand in ftyp."""
    brand = read_ftyp(fh, file_size)
    if brand is None:
        return None
    if brand in HEIF_BRANDS:
        return "heif"
    if brand in QT_BRANDS:
        return "quicktime"
    return None


# --- HEIF: locating the Exif item -------------------------------------------

def _parse_iinf(fh, box: Box) -> dict[int, bytes]:
    """{item_ID: item_type} from the iinf box."""
    fh.seek(box.start)
    head = fh.read(4)
    if len(head) < 4:
        return {}
    version = head[0]
    pos = box.start + 4
    fh.seek(pos)
    if version == 0:
        raw = fh.read(2)
        if len(raw) < 2:
            return {}
        pos += 2
    else:
        raw = fh.read(4)
        if len(raw) < 4:
            return {}
        pos += 4

    items: dict[int, bytes] = {}
    for entry in iter_boxes(fh, pos, box.end):
        if entry.type != b"infe":
            continue
        fh.seek(entry.start)
        data = fh.read(min(entry.size, 64))
        if len(data) < 4:
            continue
        ver = data[0]
        if ver >= 2:
            if ver == 2 and len(data) >= 12:
                item_id = struct.unpack_from(">H", data, 4)[0]
                item_type = data[8:12]
            elif ver >= 3 and len(data) >= 14:
                item_id = struct.unpack_from(">I", data, 4)[0]
                item_type = data[10:14]
            else:
                continue
            items[item_id] = item_type
    return items


def _parse_iloc(fh, box: Box) -> dict[int, tuple[int, list[tuple[int, int]]]]:
    """{item_ID: (construction_method, [(offset, length), ...])} from iloc."""
    fh.seek(box.start)
    data = fh.read(box.size)
    if len(data) < 8:
        return {}
    version = data[0]
    pos = 4
    offset_size, length_size = data[pos] >> 4, data[pos] & 0xF
    base_offset_size = data[pos + 1] >> 4
    index_size = data[pos + 1] & 0xF if version in (1, 2) else 0
    pos += 2

    def take(width: int) -> int | None:
        nonlocal pos
        if width == 0:
            return 0
        if pos + width > len(data):
            return None
        value = int.from_bytes(data[pos:pos + width], "big")
        pos += width
        return value

    if version < 2:
        count = take(2)
    else:
        count = take(4)
    if count is None:
        return {}

    result: dict[int, tuple[int, list[tuple[int, int]]]] = {}
    for _ in range(min(count, MAX_BOXES_PER_LEVEL)):
        item_id = take(2) if version < 2 else take(4)
        if item_id is None:
            break
        construction = 0
        if version in (1, 2):
            flags = take(2)
            if flags is None:
                break
            construction = flags & 0xF
        if take(2) is None:  # data_reference_index
            break
        base_offset = take(base_offset_size)
        extent_count = take(2)
        if base_offset is None or extent_count is None:
            break
        extents: list[tuple[int, int]] = []
        broken = False
        for _ in range(min(extent_count, 256)):
            if index_size:
                if take(index_size) is None:
                    broken = True
                    break
            off = take(offset_size)
            length = take(length_size)
            if off is None or length is None:
                broken = True
                break
            extents.append((base_offset + off, length))
        if broken:
            break
        result[item_id] = (construction, extents)
    return result


def heif_exif_block(fh, file_size: int) -> bytes | None:
    """Return the TIFF/EXIF block from a HEIC/HEIF/AVIF file."""
    meta = find_box(fh, 0, file_size, b"meta")
    if meta is None:
        return None
    # meta is a FullBox: skip version+flags before its children.
    children_start = meta.start + 4
    iinf = find_box(fh, children_start, meta.end, b"iinf")
    iloc = find_box(fh, children_start, meta.end, b"iloc")
    if iinf is None or iloc is None:
        return None
    items = _parse_iinf(fh, iinf)
    exif_ids = [i for i, t in items.items() if t == b"Exif"]
    if not exif_ids:
        return None
    locations = _parse_iloc(fh, iloc)
    idat = find_box(fh, children_start, meta.end, b"idat")

    for item_id in exif_ids:
        entry = locations.get(item_id)
        if not entry:
            continue
        construction, extents = entry
        chunks = []
        for offset, length in extents:
            if length <= 0 or length > 64 * 1024 * 1024:
                continue
            if construction == 1:
                if idat is None:
                    continue
                fh.seek(idat.start + offset)
            elif construction == 0:
                fh.seek(offset)
            else:
                continue
            chunks.append(fh.read(length))
        payload = b"".join(chunks)
        if len(payload) < 8:
            continue
        # The first 4 bytes hold the offset to the TIFF header.
        (tiff_offset,) = struct.unpack_from(">I", payload, 0)
        start = 4 + tiff_offset
        if 0 <= start < len(payload):
            return payload[start:]
    return None


# --- QuickTime / MP4 --------------------------------------------------------

_ISO6709 = re.compile(r"([+-]\d{1,3}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)")


def parse_iso6709(text: str):
    """'+44.4268+026.1025+075.000/' -> (44.4268, 26.1025)."""
    if not text:
        return None
    match = _ISO6709.search(text)
    if not match:
        return None
    try:
        lat, lon = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None
    return lat, lon


def _parse_udta_text(fh, box: Box) -> str | None:
    """Atoms such as (c)day / (c)xyz: [len u16][lang u16][text]."""
    fh.seek(box.start)
    data = fh.read(min(box.size, 1024))
    if len(data) < 4:
        return None
    (text_len,) = struct.unpack_from(">H", data, 0)
    text = data[4:4 + text_len] if 4 + text_len <= len(data) else data[4:]
    return text.decode("utf-8", "replace").strip("\x00").strip()


def _parse_keys_ilst(fh, meta_start: int, meta_end: int) -> dict[str, str]:
    """Modern Apple metadata: the keys box names them, ilst holds the values."""
    keys_box = find_box(fh, meta_start, meta_end, b"keys")
    ilst_box = find_box(fh, meta_start, meta_end, b"ilst")
    if keys_box is None or ilst_box is None:
        return {}

    fh.seek(keys_box.start)
    data = fh.read(keys_box.size)
    if len(data) < 8:
        return {}
    (entry_count,) = struct.unpack_from(">I", data, 4)
    names: list[str] = []
    pos = 8
    for _ in range(min(entry_count, 512)):
        if pos + 8 > len(data):
            break
        (size,) = struct.unpack_from(">I", data, pos)
        if size < 8 or pos + size > len(data):
            break
        names.append(data[pos + 8:pos + size].decode("utf-8", "replace"))
        pos += size

    values: dict[str, str] = {}
    for index, item in enumerate(iter_boxes(fh, ilst_box.start, ilst_box.end), start=1):
        key_index = int.from_bytes(item.type, "big")
        name = None
        if 1 <= key_index <= len(names):
            name = names[key_index - 1]
        elif index <= len(names):
            name = names[index - 1]
        if not name:
            continue
        data_box = find_box(fh, item.start, item.end, b"data")
        if data_box is None or data_box.size <= 8:
            continue
        fh.seek(data_box.start + 8)
        raw = fh.read(min(data_box.size - 8, 1024))
        values[name] = raw.decode("utf-8", "replace").strip("\x00").strip()
    return values


def _meta_children_start(fh, box: Box) -> int:
    """moov/meta is a FullBox in ISO-BMFF but not in classic QuickTime."""
    fh.seek(box.start)
    head = fh.read(12)
    if len(head) >= 8 and head[4:8].isalpha():
        return box.start
    return box.start + 4


def quicktime_metadata(fh, file_size: int) -> dict:
    """Creation date and coordinates from a MOV/MP4.

    Apple's 'creationdate' and the (c)day atom are preferred because they carry
    a UTC offset, so they give the real local time of the recording. mvhd is the
    last resort: its timestamp is UTC and we do not know where the phone was.
    """
    out: dict = {"taken_at": None, "taken_at_source": "", "gps": None,
                 "gps_source": "", "make": None, "model": None}
    moov = find_box(fh, 0, file_size, b"moov")
    if moov is None:
        return out

    meta = find_box(fh, moov.start, moov.end, b"meta")
    if meta is not None:
        values = _parse_keys_ilst(fh, _meta_children_start(fh, meta), meta.end)
        created = values.get("com.apple.quicktime.creationdate")
        if created:
            dt = _parse_iso_datetime(created)
            if dt:
                out["taken_at"], out["taken_at_source"] = dt, "qt:creationdate"
        loc = values.get("com.apple.quicktime.location.ISO6709")
        if loc:
            coords = parse_iso6709(loc)
            if coords:
                out["gps"], out["gps_source"] = coords, "qt:location"
        out["make"] = values.get("com.apple.quicktime.make") or None
        out["model"] = values.get("com.apple.quicktime.model") or None

    udta = find_box(fh, moov.start, moov.end, b"udta")
    if udta is not None:
        for box in iter_boxes(fh, udta.start, udta.end):
            if box.type == b"\xa9day" and out["taken_at"] is None:
                dt = _parse_iso_datetime(_parse_udta_text(fh, box) or "")
                if dt:
                    out["taken_at"], out["taken_at_source"] = dt, "qt:day"
            elif box.type == b"\xa9xyz" and out["gps"] is None:
                coords = parse_iso6709(_parse_udta_text(fh, box) or "")
                if coords:
                    out["gps"], out["gps_source"] = coords, "qt:xyz"

    if out["taken_at"] is None:
        mvhd = find_box(fh, moov.start, moov.end, b"mvhd")
        if mvhd is not None:
            dt = _parse_mvhd(fh, mvhd)
            if dt:
                out["taken_at"], out["taken_at_source"] = dt, "qt:mvhd"
    return out


def _parse_mvhd(fh, box: Box):
    fh.seek(box.start)
    data = fh.read(min(box.size, 32))
    if len(data) < 12:
        return None
    version = data[0]
    try:
        if version == 1:
            (created,) = struct.unpack_from(">Q", data, 4)
        else:
            (created,) = struct.unpack_from(">I", data, 4)
    except struct.error:
        return None
    if created <= 0:
        return None
    try:
        return QT_EPOCH + timedelta(seconds=created)
    except OverflowError:
        return None


def _parse_iso_datetime(text: str):
    """Accepts 2019-07-12T18:30:42+0300 and its variants."""
    text = (text or "").strip()
    if not text:
        return None
    candidate = text.replace("/", "-")
    if len(candidate) >= 5 and candidate[-5] in "+-" and candidate[-3] != ":":
        candidate = candidate[:-2] + ":" + candidate[-2:]
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    for attempt in (candidate, candidate[:19], candidate[:10]):
        try:
            return datetime.fromisoformat(attempt)
        except ValueError:
            continue
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate[:len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None
