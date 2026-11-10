"""Pairing sidecar contents with sidecar names: by entry name locally, by listed size over ADB."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import google_takeout_metadata_fixer as fixer
from tests.fake_adb import FakeAdb
from tests.takeout_fixtures import sidecar, write_zip

ALBUM = "Takeout/Google Photos/Album/"
REMOTE = "/sdcard/Download/takeout-001.zip"


def _name(i):
    return f"{ALBUM}IMG_{i:02d}.jpg.supplemental-metadata.json"


def _members(count=40, empty=(5,), array=(9,), with_media=None):
    members = {}
    for i in range(count):
        if with_media is not None:
            members[f"{ALBUM}IMG_{i:02d}.jpg"] = with_media
        if i in empty:
            members[_name(i)] = b""
        elif i in array:
            members[_name(i)] = b'[{"x": 1}, {"y": 2}]'
        else:
            # Pretty-printed like real Takeout sidecars, so line-ending translation changes the length
            data = sidecar(timestamp=1_000_000_000 + i * 86400, title=f"IMG_{i:02d}.jpg", description="brace } { inside")
            members[_name(i)] = json.dumps(data, indent=2).encode()
    return members


def _stream(members):
    sized, chunks = [], []
    for name, payload in members.items():
        if name.endswith(".json"):
            data = json.dumps(payload).encode() if isinstance(payload, dict) else payload
            sized.append((len(data), name))
            chunks.append(data)
    return sized, b"".join(chunks)


def _all_correct(paired):
    return all(data["title"] + ".supplemental-metadata.json" == name[len(ALBUM):] for name, data in paired.items())


def test_stream_is_sliced_by_listed_sizes():
    # Regression: pairing by position gave every sidecar after an empty one its neighbour's data
    sized, stream = _stream(_members())
    paired, method = fixer.pair_json_stream(stream, sized)
    assert method == "sizes"
    assert len(paired) == 38 and _all_correct(paired)
    assert _name(5) not in paired and _name(9) not in paired


def test_stream_with_translated_line_endings_is_paired_by_verified_order():
    members = _members(empty=(), array=())
    sized, stream = _stream(members)
    mangled = stream.replace(b"\n", b"\r\n")
    assert len(mangled) > len(stream)
    paired, method = fixer.pair_json_stream(mangled, sized)
    assert method == "order"
    assert len(paired) == 40 and _all_correct(paired)


def test_unverifiable_stream_is_not_paired_at_all():
    # Sizes do not add up and one sidecar is missing from the stream: any pairing would be a guess
    sized, stream = _stream(_members(empty=(), array=()))
    first = sized[0][0]
    paired, method = fixer.pair_json_stream(stream[first:] + b"\r\n", sized)
    assert (paired, method) == ({}, "none")

    # Same count, but the contents are shifted by one: titles expose it
    members = _members(count=6, empty=(), array=())
    sized, stream = _stream(members)
    rotated = stream[sized[0][0]:] + stream[: sized[0][0]] + b"\n"
    assert fixer.pair_json_stream(rotated, sized) == ({}, "none")


def test_legacy_function_no_longer_pairs_when_counts_differ():
    # Regression: 39 objects for 40 names were paired "sequentially" (the within-5% rule)
    sized, stream = _stream(_members(array=()))
    assert fixer.parse_concatenated_jsons(stream, [name for _, name in sized]) == {}


@pytest.mark.parametrize(
    ("json_name", "title", "expected"),
    [
        ("IMG_1.jpg.supplemental-metadata.json", "IMG_1.jpg", True),
        ("IMG_1.jpg.supplemental-metadata(1).json", "IMG_1.jpg", True),
        ("IMG_1.jpg.suppl.json", "IMG_1.jpg", True),
        ("IMG_1.jpg.json", "IMG_1.jpg", True),
        ("IMG_1.jpg(2).json", "IMG_1.jpg", True),
        ("IMG_1.json", "IMG_1.jpg", True),
        ("Mum_s _best_ photo.jpg.supplemental-metadata.json", "Mum's \"best\" photo.jpg", True),
        ("family_reunion_summer_lake_house_2019_group_ph.json", "family_reunion_summer_lake_house_2019_group_photo_A.jpg", True),
        ("IMG_2.jpg.supplemental-metadata.json", "IMG_1.jpg", False),
        ("IMG_1.json", "IMG_10.jpg", False),
        ("IMG_1.jpg.supplemental-metadata.json", "IMG_1.png", False),
    ],
)
def test_sidecar_name_matches_title(json_name, title, expected):
    assert fixer.sidecar_name_matches_title(ALBUM + json_name, title) is expected


def test_local_sidecars_are_read_by_name(tmp_path):
    zpath = write_zip(tmp_path / "t.zip", _members(count=12))
    names = [_name(i) for i in range(12)] + [ALBUM + "missing.json"]
    paired = fixer.local_read_jsons(str(zpath), names)
    assert len(paired) == 10 and _all_correct(paired)


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    fixer.main()


def _dates(out, indexes):
    result = subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", *[str(out / f"IMG_{i:02d}.jpg") for i in indexes]],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return [d.get("DateTimeOriginal") for d in json.loads(result.stdout)]


def _expected(i):
    return fixer._timestamp_to_exif(1_000_000_000 + i * 86400)


@pytest.mark.exiftool
def test_local_zip_with_an_empty_sidecar_end_to_end(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Regression: files 5..38 received the date of the following file, reported as fixed=40 err=0
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    write_zip(zips / "takeout-001.zip", _members(array=(), with_media=jpeg_bytes))
    for i in range(40):
        (out / f"IMG_{i:02d}.jpg").write_bytes(jpeg_bytes)
    monkeypatch.chdir(tmp_path)

    _run(monkeypatch, ["--output", str(out), "--local-zips", str(zips)])

    assert "fixed=39 no_json=1" in capsys.readouterr().out
    indexes = [4, 5, 6, 7, 38, 39]
    assert _dates(out, indexes) == [None if i == 5 else _expected(i) for i in indexes]


@pytest.mark.exiftool
@pytest.mark.parametrize("crlf", [False, True])
def test_adb_mode_pairs_sidecars_correctly_end_to_end(tmp_path, monkeypatch, capsys, jpeg_bytes, crlf):
    out = tmp_path / "out"
    out.mkdir()
    local_zip = write_zip(tmp_path / "phone.zip", _members(count=12, empty=(3,), array=(7,), with_media=jpeg_bytes))
    for i in range(12):
        (out / f"IMG_{i:02d}.jpg").write_bytes(jpeg_bytes)
    monkeypatch.chdir(tmp_path)
    adb = FakeAdb({REMOTE: local_zip}, crlf=crlf).install(monkeypatch)

    _run(monkeypatch, ["--output", str(out), "--phone-dir", "/sdcard/Download"])

    output = capsys.readouterr().out
    assert "fixed=10" in output
    indexes = list(range(12))
    assert _dates(out, indexes) == [None if i in (3, 7) else _expected(i) for i in indexes]
    # With line endings translated in transit the sizes cannot be trusted, and neither can
    # the order (an empty sidecar is in the stream): each sidecar is then read on its own.
    # Otherwise only the two unusable sidecars (one empty, one not an object) are re-read.
    singles = [c for c in adb.shell_commands if "unzip -p" in c and "*.json" not in c]
    assert len(singles) == (12 if crlf else 2)
