"""Resume state: what gets recorded, what is retried, and which output file an entry belongs to."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import google_takeout_metadata_fixer as fixer
from tests.conftest import read_tags
from tests.fake_adb import FakeAdb
from tests.takeout_fixtures import make_jpeg_bytes, sidecar, write_zip

ALBUM = "Takeout/Google Photos/Album/"
pytestmark = pytest.mark.exiftool


def _takeout(tmp_path, count, payload=None, extra=None):
    """A zip with `count` photos and sidecars, and an output folder holding the photos."""
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    payload = payload or make_jpeg_bytes()
    members = {}
    for i in range(count):
        members[f"{ALBUM}IMG_{i:04d}.jpg"] = payload
        members[f"{ALBUM}IMG_{i:04d}.jpg.supplemental-metadata.json"] = sidecar(timestamp=1_000_000_000 + i * 86400)
        (out / f"IMG_{i:04d}.jpg").write_bytes(payload)
    members.update(extra or {})
    write_zip(zips / "takeout-001.zip", members)
    return zips, out


def _run(monkeypatch, capsys, tmp_path, *extra, zips=None, out=None, expect=None):
    monkeypatch.chdir(tmp_path)
    argv = ["prog", "--output", str(out or tmp_path / "out")]
    argv += ["--local-zips", str(zips or tmp_path / "zips")] if "--phone-dir" not in extra else []
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    if expect is not None:
        with pytest.raises(expect):
            fixer.main()
    else:
        fixer.main()
    return capsys.readouterr().out


def _progress(tmp_path):
    return json.loads((tmp_path / fixer.PROGRESS_FILE).read_text())


def _pretend_written(items, **_):
    return [fixer.ApplyResult("written") for _ in items]


# --- progress is recorded as work is done -------------------------------------------------


def test_interrupted_run_resumes_where_it_stopped(tmp_path, monkeypatch, capsys):
    # Regression: progress was only written at the end of a zip, so Ctrl-C lost all of it
    _takeout(tmp_path, 130)
    calls = {"n": 0}

    def interrupt_on_third_batch(items, **_):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return [fixer.ApplyResult("written") for _ in items]

    monkeypatch.setattr(fixer, "apply_metadata_batch", interrupt_on_third_batch)
    _run(monkeypatch, capsys, tmp_path, expect=KeyboardInterrupt)
    assert len(_progress(tmp_path)["processed_entries"]) == 100
    assert _progress(tmp_path)["completed_zips"] == []

    monkeypatch.setattr(fixer, "apply_metadata_batch", _pretend_written)
    output = _run(monkeypatch, capsys, tmp_path)
    assert "fixed=30 no_json=0 no_match=0 skip=100" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]


def test_a_killed_run_is_recovered_from_the_journal(tmp_path, monkeypatch, capsys):
    # A hard kill skips the orderly save; finished entries are still in the journal
    _takeout(tmp_path, 60)
    monkeypatch.setattr(fixer, "apply_metadata_batch", _pretend_written)
    monkeypatch.setattr(fixer.ProgressStore, "checkpoint", lambda self: None)
    _run(monkeypatch, capsys, tmp_path)
    assert not (tmp_path / fixer.PROGRESS_FILE).exists()
    assert (tmp_path / (fixer.PROGRESS_FILE + ".journal")).exists()

    monkeypatch.undo()
    monkeypatch.setattr(fixer, "apply_metadata_batch", _pretend_written)
    output = _run(monkeypatch, capsys, tmp_path)
    assert "skip=60" in output
    assert not (tmp_path / (fixer.PROGRESS_FILE + ".journal")).exists()


def test_entries_without_a_result_are_retried_and_keep_the_zip_open(tmp_path, monkeypatch, capsys):
    # Regression: a batch that timed out was recorded as done and the zip as completed
    _takeout(tmp_path, 60)
    monkeypatch.setattr(fixer, "apply_metadata_batch", lambda items, **_: [fixer.ApplyResult("unknown") for _ in items])
    output = _run(monkeypatch, capsys, tmp_path)
    assert "fixed=0" in output and "no_result=60" in output
    assert _progress(tmp_path) == {"completed_zips": [], "processed_entries": [], "failed_entries": {}, "targets": {}}

    monkeypatch.setattr(fixer, "apply_metadata_batch", _pretend_written)
    output = _run(monkeypatch, capsys, tmp_path)
    assert "fixed=60 no_json=0 no_match=0 skip=0" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]


def test_exiftool_errors_are_recorded_and_only_retried_on_request(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = _takeout(tmp_path, 2, extra={
        ALBUM + "broken.jpg": b"not a jpeg",
        ALBUM + "broken.jpg.supplemental-metadata.json": sidecar(),
        ALBUM + "clip.avi": b"RIFF\x24\x00\x00\x00AVI LIST\x04\x00\x00\x00hdrl" + b"\x00" * 16,
        ALBUM + "clip.avi.supplemental-metadata.json": sidecar(),
    })
    (out / "broken.jpg").write_bytes(b"not a jpeg")
    (out / "clip.avi").write_bytes(b"RIFF\x24\x00\x00\x00AVI LIST\x04\x00\x00\x00hdrl" + b"\x00" * 16)

    output = _run(monkeypatch, capsys, tmp_path)
    assert "fixed=2 no_json=0 no_match=0 skip=0 err=2" in output
    assert "broken.jpg: Error: Not a valid JPG" in output
    progress = _progress(tmp_path)
    # A format ExifTool cannot write must not keep the zip open forever
    assert progress["completed_zips"] == ["takeout-001.zip"]
    assert sorted(k.rsplit("/", 1)[1] for k in progress["failed_entries"]) == ["broken.jpg", "clip.avi"]

    assert "SKIP (already done)" in _run(monkeypatch, capsys, tmp_path)

    (out / "broken.jpg").write_bytes(jpeg_bytes)  # the user repaired the file
    output = _run(monkeypatch, capsys, tmp_path, "--retry-failed")
    assert "fixed=1 no_json=0 no_match=0 skip=2 err=1" in output
    assert [k.rsplit("/", 1)[1] for k in _progress(tmp_path)["failed_entries"]] == ["clip.avi"]


def test_corrupt_progress_file_is_moved_aside(tmp_path, monkeypatch, capsys):
    # Regression: a truncated progress file ended the run with a traceback
    _takeout(tmp_path, 2)
    (tmp_path / fixer.PROGRESS_FILE).write_text('{"completed_zips": ["takeout-0')

    output = _run(monkeypatch, capsys, tmp_path, "--dry-run")
    assert "fixed=2" in output
    assert (tmp_path / fixer.PROGRESS_FILE).read_text() == '{"completed_zips": ["takeout-0'  # dry run: untouched

    output = _run(monkeypatch, capsys, tmp_path)
    assert "could not be read" in output and "fixed=2" in output
    assert len(list(tmp_path.glob(fixer.PROGRESS_FILE + ".corrupt-*"))) == 1
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]


def test_progress_file_from_an_earlier_version_still_loads(tmp_path, monkeypatch, capsys):
    _takeout(tmp_path, 3)
    done = f"takeout-001.zip::{ALBUM}IMG_0000.jpg"
    (tmp_path / fixer.PROGRESS_FILE).write_text(json.dumps({"completed_zips": [], "processed_entries": [done]}))
    assert "fixed=2 no_json=0 no_match=0 skip=1" in _run(monkeypatch, capsys, tmp_path)

    (tmp_path / fixer.PROGRESS_FILE).write_text(json.dumps({"completed_zips": ["takeout-001.zip"], "processed_entries": []}))
    assert "SKIP (already done): takeout-001.zip" in _run(monkeypatch, capsys, tmp_path)


def test_unreadable_local_zip_is_reported_and_not_completed(tmp_path, monkeypatch, capsys):
    zips, _ = _takeout(tmp_path, 2)
    (zips / "takeout-000.zip").write_bytes(b"truncated download")
    output = _run(monkeypatch, capsys, tmp_path)
    assert "takeout-000.zip" in output and "FAILED" in output
    assert "fixed=2" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]
    assert "1 zip(s) were not finished" in output


def test_adb_listing_failure_does_not_complete_the_zip(tmp_path, monkeypatch, capsys):
    # Regression: a device that dropped during "unzip -l" gave 0 entries and the zip was recorded as done
    zips, out = _takeout(tmp_path, 3)
    adb = FakeAdb({"/sdcard/Download/takeout-001.zip": zips / "takeout-001.zip"}).install(monkeypatch)
    adb.fail.add("list")
    output = _run(monkeypatch, capsys, tmp_path, "--phone-dir", "/sdcard/Download")
    assert "FAILED" in output and "1 zip(s) were not finished" in output
    assert _progress(tmp_path)["completed_zips"] == []

    adb.fail.clear()
    output = _run(monkeypatch, capsys, tmp_path, "--phone-dir", "/sdcard/Download")
    assert "fixed=3" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]


def test_help_lists_retry_failed():
    result = subprocess.run([sys.executable, fixer.__file__, "--help"], capture_output=True, text=True, timeout=60)
    assert "--retry-failed" in result.stdout


# --- which output file belongs to which zip entry ------------------------------------------

OLD, NEW = "Takeout/Google Photos/Photos from 2015/", "Takeout/Google Photos/Photos from 2021/"
PHOTO_2015, PHOTO_2021 = make_jpeg_bytes(b"synthetic photo from 2015"), make_jpeg_bytes(b"synthetic photo from 2021, larger")
TS_2015, TS_2021 = 1420113600, 1609502400
DATE_2015, DATE_2021 = "2015:01:01 12:00:00", "2021:01:01 12:00:00"


def _two_albums(tmp_path, out_files):
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    write_zip(zips / "takeout-001.zip", {
        OLD + "IMG_0001.jpg": PHOTO_2015,
        OLD + "IMG_0001.jpg.supplemental-metadata.json": sidecar(timestamp=TS_2015, people=["Person A"], title="IMG_0001.jpg"),
        NEW + "IMG_0001.jpg": PHOTO_2021,
        NEW + "IMG_0001.jpg.supplemental-metadata.json": sidecar(timestamp=TS_2021, people=["Person B"], title="IMG_0001.jpg"),
    })
    for name, data in out_files.items():
        (out / name).write_bytes(data)
    return out


def _who(path):
    tags = read_tags(path, "-ExifIFD:DateTimeOriginal", "-XMP-iptcExt:PersonInImage")
    return tags.get("ExifIFD:DateTimeOriginal"), tags.get("XMP-iptcExt:PersonInImage")


def test_same_name_in_two_albums_untouched_files_are_told_apart_by_size(tmp_path, monkeypatch, capsys):
    # Regression: the 2021 sidecar was written onto the 2015 photo and IMG_0001_1.jpg got nothing
    out = _two_albums(tmp_path, {"IMG_0001.jpg": PHOTO_2015, "IMG_0001_1.jpg": PHOTO_2021})
    assert "fixed=2" in _run(monkeypatch, capsys, tmp_path)
    assert _who(out / "IMG_0001.jpg") == (DATE_2015, "Person A")
    assert _who(out / "IMG_0001_1.jpg") == (DATE_2021, "Person B")


def test_same_name_in_two_albums_dated_files_are_told_apart_by_date(tmp_path, monkeypatch, capsys):
    # The extractor already wrote dates (so sizes no longer match); swap the names to be sure
    # it is the embedded date, not the name, that decides.
    out = _two_albums(tmp_path, {"IMG_0001.jpg": PHOTO_2021, "IMG_0001_1.jpg": PHOTO_2015})
    fixer.run_exiftool_batch([
        (out / "IMG_0001.jpg", [f"-DateTimeOriginal={DATE_2021}"]),
        (out / "IMG_0001_1.jpg", [f"-DateTimeOriginal={DATE_2015}"]),
    ])
    assert "fixed=2" in _run(monkeypatch, capsys, tmp_path)
    assert _who(out / "IMG_0001.jpg") == (DATE_2021, "Person B")
    assert _who(out / "IMG_0001_1.jpg") == (DATE_2015, "Person A")


def test_unidentifiable_same_name_files_are_left_alone(tmp_path, monkeypatch, capsys):
    out = _two_albums(tmp_path, {"IMG_0001.jpg": PHOTO_2015 + b"\x00", "IMG_0001_1.jpg": PHOTO_2021 + b"\x00"})
    before = [(out / n).read_bytes() for n in ("IMG_0001.jpg", "IMG_0001_1.jpg")]
    output = _run(monkeypatch, capsys, tmp_path)
    assert "fixed=0" in output and "ambiguous=2" in output
    assert [(out / n).read_bytes() for n in ("IMG_0001.jpg", "IMG_0001_1.jpg")] == before


def test_same_photo_listed_in_two_albums_shares_one_file(tmp_path, monkeypatch, capsys):
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    same = sidecar(timestamp=TS_2015, people=["Person A"], title="IMG_0001.jpg")
    write_zip(zips / "takeout-001.zip", {
        OLD + "IMG_0001.jpg": PHOTO_2015, OLD + "IMG_0001.jpg.supplemental-metadata.json": same,
        "Takeout/Google Photos/Trip/IMG_0001.jpg": PHOTO_2015,
        "Takeout/Google Photos/Trip/IMG_0001.jpg.supplemental-metadata.json": same,
    })
    (out / "IMG_0001.jpg").write_bytes(PHOTO_2015)
    output = _run(monkeypatch, capsys, tmp_path)
    assert "ambiguous" not in output.split("DONE:")[1].splitlines()[0]
    assert _who(out / "IMG_0001.jpg") == (DATE_2015, "Person A")


def test_renamed_copy_is_used_when_the_plain_name_is_absent(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    write_zip(zips / "takeout-001.zip", {
        ALBUM + "IMG_5.jpg": jpeg_bytes, ALBUM + "IMG_5.jpg.supplemental-metadata.json": sidecar(timestamp=TS_2015),
    })
    (out / "IMG_5_1.jpg").write_bytes(jpeg_bytes)
    assert "fixed=1" in _run(monkeypatch, capsys, tmp_path)
    assert _who(out / "IMG_5_1.jpg")[0] == DATE_2015
