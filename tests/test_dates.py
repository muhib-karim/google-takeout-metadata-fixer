"""Timestamp handling: sidecar parsing, time zones, and when an embedded capture time is kept."""

from __future__ import annotations

import subprocess
import sys

import pytest

import google_takeout_metadata_fixer as fixer
from tests.conftest import read_tags
from tests.takeout_fixtures import sidecar, write_zip

UTC_TEXT = "2020:09:13 12:26:40"  # 1600000000 seconds after the epoch, in UTC

# --- parsing ---------------------------------------------------------------------------


def test_pre_1970_capture_time_is_kept():
    # Regression: negative timestamps were discarded and the upload time was written instead
    data = {"photoTakenTime": {"timestamp": "-157766400"}, "creationTime": {"timestamp": "1600000000"}}
    assert fixer.parse_takeout_json(data)["datetime"] == "1965:01:01 00:00:00"


def test_zero_timestamp_still_means_unknown():
    data = {"photoTakenTime": {"timestamp": "0"}, "creationTime": {"timestamp": "1600000000"}}
    assert fixer.parse_takeout_json(data)["datetime"] == UTC_TEXT


@pytest.mark.parametrize(
    "data",
    [
        {"photoTakenTime": None},
        {"photoTakenTime": "1600000000"},
        {"photoTakenTime": {"timestamp": "99999999999999999"}},
        {"photoTakenTime": {"timestamp": True}},
        {"geoData": {"latitude": None, "longitude": 5}},
        {"geoData": {"latitude": "12.5", "longitude": 5}},
        {"geoData": {"latitude": 91.0, "longitude": 5.0}},
        {"geoData": {"latitude": 5.0, "longitude": float("nan")}},
        {"geoData": [1, 2]},
        {"people": [None, "Sam", {"name": 7}, {"name": "  "}]},
        {"people": "Sam"},
        {"description": 5},
        {"title": ["x"]},
    ],
)
def test_malformed_fields_are_skipped_not_fatal(data):
    # Regression: each of these raised and aborted the whole run
    assert fixer.parse_takeout_json(data) == {}
    assert fixer.build_exiftool_args(fixer.parse_takeout_json(data), "a.jpg") == []


@pytest.mark.parametrize("data", [None, [], "text", 7, [{"photoTakenTime": {"timestamp": "1"}}]])
def test_non_object_sidecar_gives_no_metadata(data):
    assert fixer.parse_takeout_json(data) == {}


def test_good_fields_survive_next_to_bad_ones():
    data = {"photoTakenTime": {"timestamp": "1600000000"}, "geoData": {"latitude": None}, "people": [{"name": " Sam "}]}
    assert fixer.parse_takeout_json(data) == {"datetime": UTC_TEXT, "people": ["Sam"]}


# --- argument building -----------------------------------------------------------------


def test_photo_dates_carry_an_explicit_utc_offset():
    args = fixer.build_exiftool_args({"datetime": UTC_TEXT}, "a.jpg")
    assert args == [
        f"-DateTimeOriginal={UTC_TEXT}",
        f"-CreateDate={UTC_TEXT}",
        f"-ModifyDate={UTC_TEXT}",
        "-OffsetTimeOriginal=+00:00",
        "-OffsetTimeDigitized=+00:00",
        "-OffsetTime=+00:00",
    ]


def test_dates_can_be_left_out():
    metadata = {"datetime": UTC_TEXT, "description": "x"}
    assert fixer.build_exiftool_args(metadata, "a.jpg", write_dates=False) == ["-ImageDescription=x"]
    assert fixer.build_exiftool_args({"datetime": UTC_TEXT}, "a.jpg", write_dates=False) == []


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("2020:09:13 14:26:40", True),  # two hours ahead of UTC
        ("2020:09:13 12:26:40", True),  # already UTC
        ("2020:09:13 17:56:40", True),  # +05:30
        ("2020:09:13 06:41:40", True),  # -05:45
        ("2020:09:14 02:26:40", True),  # +14:00, the largest real offset
        ("2020:09:13 00:26:40", True),  # -12:00
        ("2020:09:13 14:26:41", True),  # one second of rounding
        ("2020:09:13 14:26:40.123+02:00", True),  # XMP form with fraction and zone
        ("2020:09:13 14:31:40", False),  # five minutes off: not a time-zone shift
        ("2020:09:14 03:26:40", False),  # +15:00 does not exist
        ("2020:09:16 12:26:40", False),  # three days later
        ("2019:09:13 12:26:40", False),
        ("0000:00:00 00:00:00", False),
        ("not a date", False),
        ("", False),
        (None, False),
    ],
)
def test_camera_time_matches(existing, expected):
    assert fixer.camera_time_matches(existing, UTC_TEXT) is expected


# --- behaviour against the real binary --------------------------------------------------

DATE_TAGS = ("-ExifIFD:DateTimeOriginal", "-ExifIFD:OffsetTimeOriginal", "-IFD0:ImageDescription")


def _jpeg_with(tmp_path, jpeg_bytes, *exif_args, name="a.jpg"):
    target = tmp_path / name
    target.write_bytes(jpeg_bytes)
    if exif_args:
        assert fixer.run_exiftool_batch([(target, list(exif_args))]) == (1, 0)
    return target


@pytest.mark.exiftool
def test_consistent_camera_time_is_kept(tmp_path, jpeg_bytes):
    # Regression: local time 14:26:40 +02:00 was overwritten with 12:26:40 while the +02:00
    # tag stayed, which moved the photo two hours.
    target = _jpeg_with(tmp_path, jpeg_bytes, "-DateTimeOriginal=2020:09:13 14:26:40", "-OffsetTimeOriginal=+02:00")
    metadata = {"datetime": UTC_TEXT, "description": "caption"}

    results = fixer.apply_metadata_batch([(target, metadata)])

    assert [(r.status, r.date_kept) for r in results] == [("written", True)]
    assert read_tags(target, *DATE_TAGS) == {
        "ExifIFD:DateTimeOriginal": "2020:09:13 14:26:40",
        "ExifIFD:OffsetTimeOriginal": "+02:00",
        "IFD0:ImageDescription": "caption",
    }


@pytest.mark.exiftool
def test_nothing_is_written_when_only_the_date_is_known_and_it_is_consistent(tmp_path, jpeg_bytes):
    target = _jpeg_with(tmp_path, jpeg_bytes, "-DateTimeOriginal=2020:09:13 14:26:40")
    before = target.read_bytes()
    results = fixer.apply_metadata_batch([(target, {"datetime": UTC_TEXT})])
    assert [(r.status, r.date_kept) for r in results] == [("kept", True)]
    assert target.read_bytes() == before


@pytest.mark.exiftool
def test_missing_date_is_written_with_utc_offset(tmp_path, jpeg_bytes):
    target = _jpeg_with(tmp_path, jpeg_bytes)
    results = fixer.apply_metadata_batch([(target, {"datetime": UTC_TEXT})])
    assert [(r.status, r.date_kept) for r in results] == [("written", False)]
    tags = read_tags(target, *DATE_TAGS, "-Composite:SubSecDateTimeOriginal")
    assert tags["ExifIFD:DateTimeOriginal"] == UTC_TEXT
    assert tags["ExifIFD:OffsetTimeOriginal"] == "+00:00"
    assert tags["Composite:SubSecDateTimeOriginal"] == UTC_TEXT + "+00:00"


@pytest.mark.exiftool
def test_conflicting_date_is_replaced_and_its_offset_corrected(tmp_path, jpeg_bytes):
    # A date edited in Google Photos differs by more than any time zone: the sidecar wins
    target = _jpeg_with(tmp_path, jpeg_bytes, "-DateTimeOriginal=2020:09:16 09:00:00", "-OffsetTimeOriginal=+02:00")
    fixer.apply_metadata_batch([(target, {"datetime": UTC_TEXT})])
    tags = read_tags(target, *DATE_TAGS)
    assert (tags["ExifIFD:DateTimeOriginal"], tags["ExifIFD:OffsetTimeOriginal"]) == (UTC_TEXT, "+00:00")


@pytest.mark.exiftool
def test_overwrite_dates_forces_the_sidecar_time(tmp_path, jpeg_bytes):
    target = _jpeg_with(tmp_path, jpeg_bytes, "-DateTimeOriginal=2020:09:13 14:26:40", "-OffsetTimeOriginal=+02:00")
    results = fixer.apply_metadata_batch([(target, {"datetime": UTC_TEXT})], overwrite_dates=True)
    assert [(r.status, r.date_kept) for r in results] == [("written", False)]
    tags = read_tags(target, *DATE_TAGS)
    assert (tags["ExifIFD:DateTimeOriginal"], tags["ExifIFD:OffsetTimeOriginal"]) == (UTC_TEXT, "+00:00")


@pytest.mark.exiftool
def test_mixed_batch_keeps_results_in_order(tmp_path, jpeg_bytes, mp4_bytes):
    kept = _jpeg_with(tmp_path, jpeg_bytes, "-DateTimeOriginal=2020:09:13 14:26:40", name="kept.jpg")
    fresh = _jpeg_with(tmp_path, jpeg_bytes, name="fresh.jpg")
    junk = tmp_path / "junk.jpg"
    junk.write_bytes(b"not a jpeg")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(mp4_bytes)
    items = [(p, {"datetime": UTC_TEXT}) for p in (kept, fresh, junk, clip)]

    results = fixer.apply_metadata_batch(items)

    assert [r.status for r in results] == ["kept", "written", "failed", "written"]
    assert "Not a valid JPG" in results[2].message


@pytest.mark.exiftool
@pytest.mark.skipif(sys.platform == "win32", reason="TZ is not honoured on Windows")
@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Kolkata"])
def test_video_dates_do_not_depend_on_the_machine_time_zone(tmp_path, monkeypatch, mp4_bytes, zone):
    # Regression: one tag got the local UTC offset appended (12:26:40-04:00 in New York)
    monkeypatch.setenv("TZ", zone)
    target = tmp_path / "clip.mp4"
    target.write_bytes(mp4_bytes)

    fixer.apply_metadata_batch([(target, {"datetime": UTC_TEXT})])

    tags = read_tags(target, "-UserData:DateTimeOriginal", "-QuickTime:CreateDate", "-XMP-exif:DateTimeOriginal")
    assert tags == {
        "UserData:DateTimeOriginal": UTC_TEXT + "+00:00",
        "QuickTime:CreateDate": UTC_TEXT,
        "XMP-exif:DateTimeOriginal": UTC_TEXT + "+00:00",
    }


def _takeout(tmp_path, jpeg_bytes):
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    album = "Takeout/Google Photos/Album/"
    write_zip(
        zips / "takeout-001.zip",
        {
            album + "camera.jpg": jpeg_bytes,
            album + "camera.jpg.supplemental-metadata.json": sidecar(timestamp=1600000000, description="caption"),
            album + "old.jpg": jpeg_bytes,
            album + "old.jpg.supplemental-metadata.json": sidecar(timestamp=-157766400),
        },
    )
    (out / "old.jpg").write_bytes(jpeg_bytes)
    _jpeg_with(out, jpeg_bytes, "-DateTimeOriginal=2020:09:13 14:26:40", "-OffsetTimeOriginal=+02:00", name="camera.jpg")
    return zips, out


@pytest.mark.exiftool
def test_cli_keeps_camera_time_by_default_and_reports_it(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = _takeout(tmp_path, jpeg_bytes)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", "--output", str(out), "--local-zips", str(zips)])
    fixer.main()

    output = capsys.readouterr().out
    assert "fixed=2" in output
    assert "Dates kept (camera time):   1" in output
    tags = read_tags(out / "camera.jpg", *DATE_TAGS)
    assert (tags["ExifIFD:DateTimeOriginal"], tags["ExifIFD:OffsetTimeOriginal"]) == ("2020:09:13 14:26:40", "+02:00")
    assert tags["IFD0:ImageDescription"] == "caption"
    assert read_tags(out / "old.jpg", "-ExifIFD:DateTimeOriginal")["ExifIFD:DateTimeOriginal"] == "1965:01:01 00:00:00"


@pytest.mark.exiftool
def test_cli_overwrite_dates_flag(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = _takeout(tmp_path, jpeg_bytes)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", "--output", str(out), "--local-zips", str(zips), "--overwrite-dates"])
    fixer.main()

    assert "Dates kept (camera time):   0" in capsys.readouterr().out
    tags = read_tags(out / "camera.jpg", *DATE_TAGS)
    assert (tags["ExifIFD:DateTimeOriginal"], tags["ExifIFD:OffsetTimeOriginal"]) == (UTC_TEXT, "+00:00")


def test_help_lists_the_new_flag():
    result = subprocess.run(
        [sys.executable, fixer.__file__, "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert "--overwrite-dates" in result.stdout
