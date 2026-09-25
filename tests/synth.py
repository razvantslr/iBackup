"""Constructori de fisiere de test: JPEG cu EXIF, HEIC, MOV.

Scriem containerele de mana ca sa testam exact parserele noastre, fara sa avem
nevoie de poze reale in repo si fara dependinte externe.
"""
from __future__ import annotations

import struct
from datetime import datetime


# --- TIFF / EXIF ------------------------------------------------------------

def _pack_ifd(entries, ifd_offset: int, next_ifd: int = 0) -> bytes:
    """Serializeaza un IFD (little-endian) impreuna cu zona lui de date."""
    entries = sorted(entries, key=lambda e: e[0])
    count = len(entries)
    data_start = ifd_offset + 2 + 12 * count + 4
    header = struct.pack("<H", count)
    data = b""
    for tag, typ, value in entries:
        if typ == 2:
            raw = value.encode("ascii") + b"\x00"
            items = len(raw)
        elif typ == 4:
            values = value if isinstance(value, (list, tuple)) else [value]
            raw = b"".join(struct.pack("<I", v) for v in values)
            items = len(values)
        elif typ == 5:
            raw = b"".join(struct.pack("<II", num, den) for num, den in value)
            items = len(value)
        else:
            raise ValueError(f"tip nesuportat in generator: {typ}")
        if len(raw) <= 4:
            field = raw.ljust(4, b"\x00")
        else:
            field = struct.pack("<I", data_start + len(data))
            data += raw
            if len(data) % 2:
                data += b"\x00"
        header += struct.pack("<HHI", tag, typ, items) + field
    header += struct.pack("<I", next_ifd)
    return header + data


def _dms(value: float):
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 1000)
    return [(degrees, 1), (minutes, 1), (seconds, 1000)]


def build_exif(taken_at: datetime | None = None, gps=None,
               make: str | None = "Apple", model: str | None = "iPhone 13") -> bytes:
    """Un bloc TIFF/EXIF cu data, GPS si numele camerei."""
    ifd0 = []
    if make:
        ifd0.append((0x010F, 2, make))
    if model:
        ifd0.append((0x0110, 2, model))

    exif_entries = []
    if taken_at is not None:
        stamp = taken_at.strftime("%Y:%m:%d %H:%M:%S")
        exif_entries.append((0x9003, 2, stamp))
        exif_entries.append((0x9004, 2, stamp))

    gps_entries = []
    if gps is not None:
        lat, lon = gps
        gps_entries = [
            (0x0001, 2, "N" if lat >= 0 else "S"),
            (0x0002, 5, _dms(lat)),
            (0x0003, 2, "E" if lon >= 0 else "W"),
            (0x0004, 5, _dms(lon)),
        ]

    def assemble(exif_offset: int, gps_offset: int) -> bytes:
        entries = list(ifd0)
        if exif_entries:
            entries.append((0x8769, 4, exif_offset))
        if gps_entries:
            entries.append((0x8825, 4, gps_offset))
        block0 = _pack_ifd(entries, 8)
        out = b"II" + struct.pack("<HI", 42, 8) + block0
        if exif_entries:
            out += _pack_ifd(exif_entries, len(out))
        if gps_entries:
            out += _pack_ifd(gps_entries, len(out))
        return out

    # Prima trecere doar ca sa aflam unde cad IFD-urile; dimensiunile nu se
    # schimba, pentru ca pointerii sunt LONG-uri stocate inline.
    draft = assemble(0, 0)
    block0_len = len(_pack_ifd(
        list(ifd0)
        + ([(0x8769, 4, 0)] if exif_entries else [])
        + ([(0x8825, 4, 0)] if gps_entries else []),
        8,
    ))
    exif_offset = 8 + block0_len
    exif_len = len(_pack_ifd(exif_entries, exif_offset)) if exif_entries else 0
    gps_offset = exif_offset + exif_len
    final = assemble(exif_offset, gps_offset)
    assert len(final) == len(draft)
    return final


# --- JPEG -------------------------------------------------------------------

def build_jpeg(exif_block: bytes | None = None, payload: bytes = b"\x00" * 64) -> bytes:
    out = b"\xff\xd8"
    if exif_block:
        segment = b"Exif\x00\x00" + exif_block
        out += b"\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment
    out += b"\xff\xdb" + struct.pack(">H", 2 + len(payload)) + payload
    out += b"\xff\xda" + struct.pack(">H", 2) + b"\xff\xd9"
    return out


# --- ISO-BMFF ---------------------------------------------------------------

def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def build_heic(exif_block: bytes) -> bytes:
    """HEIC minimal: ftyp + meta(iinf, iloc) + mdat cu blocul EXIF."""
    ftyp = _box(b"ftyp", b"heic" + struct.pack(">I", 0) + b"heic" + b"mif1")

    infe = _box(b"infe", bytes([2, 0, 0, 0]) + struct.pack(">HH", 1, 0)
                + b"Exif" + b"Exif\x00")
    iinf = _box(b"iinf", bytes([0, 0, 0, 0]) + struct.pack(">H", 1) + infe)

    def make_iloc(extent_offset: int) -> bytes:
        body = bytes([1, 0, 0, 0])              # version 1, flags
        body += bytes([(4 << 4) | 4])           # offset_size=4, length_size=4
        body += bytes([(0 << 4) | 0])           # base_offset_size=0, index_size=0
        body += struct.pack(">H", 1)            # item_count
        body += struct.pack(">H", 1)            # item_ID
        body += struct.pack(">H", 0)            # construction_method = 0
        body += struct.pack(">H", 0)            # data_reference_index
        body += struct.pack(">H", 1)            # extent_count
        body += struct.pack(">I", extent_offset)
        body += struct.pack(">I", len(exif_block) + 4)
        return _box(b"iloc", body)

    payload = struct.pack(">I", 0) + exif_block   # offset 0 catre antetul TIFF

    meta_stub = _box(b"meta", bytes([0, 0, 0, 0]) + iinf + make_iloc(0))
    data_offset = len(ftyp) + len(meta_stub) + 8
    meta = _box(b"meta", bytes([0, 0, 0, 0]) + iinf + make_iloc(data_offset))
    assert len(meta) == len(meta_stub)
    return ftyp + meta + _box(b"mdat", payload)


def _qt_text_box(box_type: bytes, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _box(box_type, struct.pack(">HH", len(raw), 0) + raw)


def build_mov(created: str | None = "2019-07-12T18:30:42+0300",
              iso6709: str | None = "+44.4268+026.1025+075.000/",
              mvhd_seconds: int | None = None) -> bytes:
    """MOV minimal cu atomii (c)day si (c)xyz din udta."""
    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")

    mvhd_payload = bytes([0, 0, 0, 0])
    mvhd_payload += struct.pack(">I", mvhd_seconds or 0)   # creation_time
    mvhd_payload += struct.pack(">I", 0)                   # modification_time
    mvhd_payload += struct.pack(">I", 600)                 # timescale
    mvhd_payload += struct.pack(">I", 6000)                # duration
    mvhd_payload += b"\x00" * 80
    mvhd = _box(b"mvhd", mvhd_payload)

    udta_children = b""
    if created:
        udta_children += _qt_text_box(b"\xa9day", created)
    if iso6709:
        udta_children += _qt_text_box(b"\xa9xyz", iso6709)
    udta = _box(b"udta", udta_children) if udta_children else b""

    return ftyp + _box(b"moov", mvhd + udta) + _box(b"mdat", b"\x00" * 32)


def write_photo(path, taken_at=None, gps=None, payload=b"\x00" * 64,
                make="Apple", model="iPhone 13"):
    """Scrie un JPEG de test si intoarce calea."""
    exif_block = build_exif(taken_at, gps, make, model) if (taken_at or gps) else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_jpeg(exif_block, payload))
    return path
