"""Tests for sidecar parsing, ExifTool argument building and the JSON stream splitter."""

import json

import pytest

from google_takeout_metadata_fixer import (
    build_exiftool_args,
    format_size,
    format_time,
    is_media,
    parse_concatenated_jsons,
    parse_takeout_json,
)
from tests.takeout_fixtures import sidecar


class TestParseTakeoutJson:
    def test_full_sidecar(self):
        data = sidecar(
            timestamp=1600000000, lat=48.8584, lng=2.2945, alt=35.0,
            description="  Eiffel Tower  ", people=["Alice", "Bob"], title="IMG_1.jpg",
        )
        data["favorited"] = True
        meta = parse_takeout_json(data)
        assert meta == {
            "datetime": "2020:09:13 12:26:40",
            "latitude": 48.8584,
            "longitude": 2.2945,
            "altitude": 35.0,
            "description": "Eiffel Tower",
            "people": ["Alice", "Bob"],
            "favorited": True,
            "title": "IMG_1.jpg",
        }

    def test_falls_back_to_creation_time(self):
        data = {"photoTakenTime": {"timestamp": "0"}, "creationTime": {"timestamp": "86400"}}
        assert parse_takeout_json(data)["datetime"] == "1970:01:02 00:00:00"

    def test_invalid_timestamp_is_ignored(self):
        data = {"photoTakenTime": {"timestamp": "not-a-number"}, "creationTime": {"timestamp": "60"}}
        assert parse_takeout_json(data)["datetime"] == "1970:01:01 00:01:00"

    def test_zero_gps_uses_exif_geo(self):
        data = {
            "geoData": {"latitude": 0.0, "longitude": 0.0},
            "geoDataExif": {"latitude": -33.86, "longitude": 151.21, "altitude": 5},
        }
        meta = parse_takeout_json(data)
        assert (meta["latitude"], meta["longitude"], meta["altitude"]) == (-33.86, 151.21, 5)

    def test_empty_values_produce_empty_metadata(self):
        data = {"description": "   ", "people": [{"name": ""}], "geoData": {"latitude": 0, "longitude": 0}}
        assert parse_takeout_json(data) == {}


class TestBuildExiftoolArgs:
    def test_photo_with_everything(self):
        meta = {
            "datetime": "2020:09:13 12:26:40", "latitude": -33.5, "longitude": -70.25, "altitude": -10,
            "description": "Beach", "people": ["Alice", "Bob"],
        }
        args = build_exiftool_args(meta, "IMG.jpg")
        assert args == [
            "-DateTimeOriginal=2020:09:13 12:26:40",
            "-CreateDate=2020:09:13 12:26:40",
            "-ModifyDate=2020:09:13 12:26:40",
            "-GPSLatitude=33.5",
            "-GPSLatitudeRef=S",
            "-GPSLongitude=70.25",
            "-GPSLongitudeRef=W",
            "-GPSAltitude=10",
            "-GPSAltitudeRef=1",
            "-ImageDescription=Beach",
            "-XMP:PersonInImage=Alice",
            "-XMP:PersonInImage=Bob",
        ]

    def test_video_gets_quicktime_tags(self):
        args = build_exiftool_args({"datetime": "2021:01:01 00:00:00"}, "clip.MP4")
        assert "-TrackCreateDate=2021:01:01 00:00:00" in args
        assert "-MediaModifyDate=2021:01:01 00:00:00" in args
        assert len(args) == 7

    def test_zero_altitude_omitted(self):
        args = build_exiftool_args({"latitude": 1.0, "longitude": 2.0, "altitude": 0}, "a.jpg")
        assert not any(a.startswith("-GPSAltitude") for a in args)
        assert "-GPSLatitudeRef=N" in args and "-GPSLongitudeRef=E" in args

    def test_metadata_without_writable_fields(self):
        assert build_exiftool_args({"title": "x", "favorited": True}, "a.jpg") == []


class TestParseConcatenatedJsons:
    def test_pairs_by_order(self):
        objs = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
        raw = b"".join(json.dumps(o).encode() for o in objs)
        assert parse_concatenated_jsons(raw, ["1.json", "2.json", "3.json"]) == {
            "1.json": {"title": "a"}, "2.json": {"title": "b"}, "3.json": {"title": "c"},
        }

    def test_braces_inside_strings(self):
        # Regression: the old brace counter split on "{" / "}" inside string values
        objs = [{"description": "smile :} {not json"}, {"title": "b", "nested": {"x": 1}}]
        raw = "\n".join(json.dumps(o) for o in objs).encode()
        result = parse_concatenated_jsons(raw, ["a.json", "b.json"])
        assert result["a.json"]["description"] == "smile :} {not json"
        assert result["b.json"]["nested"] == {"x": 1}

    def test_large_count_mismatch_returns_nothing(self):
        raw = json.dumps({"title": "only one"}).encode()
        assert parse_concatenated_jsons(raw, [f"{i}.json" for i in range(5)]) == {}

    def test_empty_stream(self):
        assert parse_concatenated_jsons(b"", []) == {}


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.JPG", True), ("b.heic", True), ("c.mp4", True), ("d.json", False), ("e.txt", False), ("dir/", False)],
)
def test_is_media(name, expected):
    assert is_media(name) is expected


def test_formatters():
    assert format_size(512) == "512.0 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(3 * 1024**3) == "3.0 GB"
    assert format_time(42) == "42s"
    assert format_time(125) == "2m 5s"
    assert format_time(3 * 3600 + 120) == "3h 2m"
