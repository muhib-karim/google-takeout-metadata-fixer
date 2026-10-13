"""Tests for the raw zip parsing used by recover_takeout_zip_metadata.py and the local zip helpers."""

import io
import zipfile

from google_takeout_metadata_fixer import (
    local_get_zip_listing,
    local_list_zips,
    local_stream_all_jsons,
    local_stream_single_json,
)
from recover_takeout_zip_metadata import find_eocd, parse_central_directory, parse_zip64_eocd
from tests.takeout_fixtures import sidecar, write_zip


def _zip_bytes(members, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_find_eocd_and_parse_central_directory_roundtrip():
    members = {"Takeout/a.jpg": b"\xff\xd8" + b"x" * 100, "Takeout/a.jpg.supplemental-metadata.json": b'{"t": 1}'}
    data = _zip_bytes(members)

    # Simulate reading only the tail of the file, as the recovery script does over ADB
    tail_offset = max(0, len(data) - 64)
    cd_offset, cd_size = find_eocd(data[tail_offset:], tail_offset)
    assert cd_offset is not None and cd_offset + cd_size <= len(data)

    entries = parse_central_directory(data[cd_offset:cd_offset + cd_size])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
    assert [e["name"] for e in entries] == [i.filename for i in infos]
    for entry, info in zip(entries, infos):
        assert entry["local_offset"] == info.header_offset
        assert entry["comp_size"] == info.compress_size
        assert entry["uncomp_size"] == info.file_size
        assert entry["method"] == info.compress_type


def test_find_eocd_missing_signature():
    assert find_eocd(b"\x00" * 100, 0) == (None, None)


def test_parse_central_directory_stops_on_bad_signature():
    assert parse_central_directory(b"not a central directory" * 3) == []


def test_parse_zip64_eocd_rejects_short_or_wrong_data():
    assert parse_zip64_eocd(b"PK\x06\x06" + b"\x00" * 10) == (None, None)
    assert parse_zip64_eocd(b"\x00" * 56) == (None, None)


def test_local_zip_helpers(tmp_path):
    write_zip(tmp_path / "takeout-002.zip", {"x.json": {"a": 1}})
    write_zip(
        tmp_path / "takeout-001.zip",
        {
            "Takeout/Google Photos/IMG_1.jpg": b"jpeg",
            "Takeout/Google Photos/IMG_1.jpg.supplemental-metadata.json": sidecar(title="IMG_1.jpg"),
            "Takeout/Google Photos/IMG_2.jpg.json": sidecar(timestamp=1),
        },
    )
    (tmp_path / "notes.txt").write_text("ignore me")

    assert local_list_zips(tmp_path) == ["takeout-001.zip", "takeout-002.zip"]

    zpath = str(tmp_path / "takeout-001.zip")
    listing = local_get_zip_listing(zpath)
    assert [name for _, name in listing] == [
        "Takeout/Google Photos/IMG_1.jpg",
        "Takeout/Google Photos/IMG_1.jpg.supplemental-metadata.json",
        "Takeout/Google Photos/IMG_2.jpg.json",
    ]
    assert listing[0][0] == 4

    stream = local_stream_all_jsons(zpath)
    assert stream.count(b"photoTakenTime") == 2
    assert b'"title": "IMG_1.jpg"' in local_stream_single_json(zpath, listing[1][1])
