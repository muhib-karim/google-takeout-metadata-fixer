"""Helpers for building tiny synthetic Google Takeout fixtures.

Everything here is generated: no real photos, locations or people are stored in the repo,
and no imaging library is needed to run the tests.
"""

from __future__ import annotations

import base64
import json
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

# An 8x8 single-colour JPEG with no EXIF data. It was generated once with Pillow
# (``Image.new("RGB", (8, 8), (200, 80, 40))``) and is embedded so the suite does not
# depend on an imaging library.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAx"
    "NDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
    "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm"
    "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
    "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDOooor5s+4P//Z"
)


def make_jpeg_bytes(comment: bytes = b"") -> bytes:
    """Return the bytes of a tiny valid JPEG with no EXIF data.

    ``comment`` is stored in a JPEG comment segment, which gives tests a way to make
    files with different contents and sizes ("two different photos").
    """
    if not comment:
        return _TINY_JPEG
    segment = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return _TINY_JPEG[:2] + segment + _TINY_JPEG[2:]


def make_png_bytes() -> bytes:
    """Return the bytes of an 8x8 single-colour PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x28\x50\xc8" * 8 for _ in range(8))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def make_mp4_bytes() -> bytes:
    """Return the bytes of a minimal MP4 container (no tracks) that ExifTool can write to."""

    def box(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", 8 + len(data)) + kind + data

    mvhd = (
        struct.pack(">IIIII", 0, 0, 0, 1000, 0)
        + struct.pack(">IH", 0x00010000, 0x0100)
        + b"\x00" * 10
        + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
        + struct.pack(">I", 1)
    )
    return box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + box(b"moov", box(b"mvhd", mvhd)) + box(b"mdat", b"")


def sidecar(
    timestamp: int = 1600000000,
    lat: float = 0.0,
    lng: float = 0.0,
    alt: float = 0.0,
    description: str = "",
    people: tuple[str, ...] | list[str] = (),
    title: str | None = None,
) -> dict[str, Any]:
    """Build a dict shaped like a Google Takeout JSON sidecar."""
    data: dict[str, Any] = {
        "title": title,
        "description": description,
        "photoTakenTime": {"timestamp": str(timestamp), "formatted": ""},
        "geoData": {"latitude": lat, "longitude": lng, "altitude": alt},
        "people": [{"name": n} for n in people],
    }
    if title is None:
        del data["title"]
    return data


def write_zip(path: Path | str, members: dict[str, Any]) -> Path | str:
    """Write a zip at ``path`` from a {name: bytes|str|dict} mapping (dicts are JSON-encoded)."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            if isinstance(payload, dict):
                payload = json.dumps(payload).encode("utf-8")
            zf.writestr(name, payload)
    return path
