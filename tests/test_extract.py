"""Tests for extract_takeout_media.py: what ends up in the output folder, and resume."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import extract_takeout_media as extractor
import google_takeout_metadata_fixer as fixer
from tests.conftest import read_tags
from tests.takeout_fixtures import sidecar, write_zip

ALBUM = "Takeout/Google Photos/Album/"
pytestmark = pytest.mark.exiftool


def _run(monkeypatch, capsys, tmp_path, *extra, expect=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path / "zips"), "--output", str(tmp_path / "out"), *extra])
    if expect is not None:
        with pytest.raises(expect):
            extractor.main()
    else:
        extractor.main()
    return capsys.readouterr().out


def _zip(tmp_path, members, name="takeout-001.zip"):
    (tmp_path / "zips").mkdir(exist_ok=True)
    return write_zip(tmp_path / "zips" / name, members)


def _progress(tmp_path):
    return json.loads((tmp_path / extractor.PROGRESS_FILE).read_text())


def _files(tmp_path):
    return sorted(p.name for p in (tmp_path / "out").iterdir())


def test_media_is_extracted_even_when_its_sidecar_is_unusable(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Regression: a malformed sidecar raised, and the photo itself was never written to the output
    _zip(tmp_path, {
        ALBUM + "good.jpg": jpeg_bytes, ALBUM + "good.jpg.supplemental-metadata.json": sidecar(),
        ALBUM + "bad_json.jpg": jpeg_bytes, ALBUM + "bad_json.jpg.supplemental-metadata.json": b"{ truncated",
        ALBUM + "bad_ts.jpg": jpeg_bytes,
        ALBUM + "bad_ts.jpg.supplemental-metadata.json": {"photoTakenTime": {"timestamp": "n/a"}},
        ALBUM + "array.jpg": jpeg_bytes, ALBUM + "array.jpg.supplemental-metadata.json": b"[1, 2]",
    })
    output = _run(monkeypatch, capsys, tmp_path)
    assert _files(tmp_path) == ["array.jpg", "bad_json.jpg", "bad_ts.jpg", "good.jpg"]
    assert "1 fixed, 3 no-metadata, 0 errors" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]


def test_unreadable_zip_is_not_marked_completed(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Regression: a truncated download was recorded as completed and never looked at again
    _zip(tmp_path, {ALBUM + "a.jpg": jpeg_bytes})
    (tmp_path / "zips" / "takeout-000.zip").write_bytes(b"truncated download, not a zip")
    output = _run(monkeypatch, capsys, tmp_path)
    assert "takeout-000.zip" in output and "left unfinished" in output
    assert _progress(tmp_path)["completed_zips"] == ["takeout-001.zip"]
    assert _files(tmp_path) == ["a.jpg"]


def test_interrupted_run_resumes_without_duplicating_files(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Regression: after an interrupt the re-run extracted finished files again as name_1.jpg
    members = {}
    for i in range(12):
        members[f"{ALBUM}IMG_{i:02d}.jpg"] = jpeg_bytes
        members[f"{ALBUM}IMG_{i:02d}.jpg.supplemental-metadata.json"] = sidecar(timestamp=1_000_000_000 + i)
    _zip(tmp_path, members)
    real = fixer.apply_metadata_batch
    calls = {"n": 0}

    def interrupt_on_eighth(items, **kwargs):
        calls["n"] += 1
        if calls["n"] == 8:
            raise KeyboardInterrupt
        return real(items, **kwargs)

    monkeypatch.setattr(extractor, "apply_metadata_batch", interrupt_on_eighth)
    _run(monkeypatch, capsys, tmp_path, expect=KeyboardInterrupt)
    assert len(_files(tmp_path)) == 7  # nothing half-written, no staging folder left behind

    monkeypatch.setattr(extractor, "apply_metadata_batch", real)
    output = _run(monkeypatch, capsys, tmp_path)
    assert _files(tmp_path) == [f"IMG_{i:02d}.jpg" for i in range(12)]
    assert "5 fixed, 0 no-metadata, 0 errors, 7 skipped" in output


def test_extractor_uses_the_shared_matcher_and_writer(tmp_path, monkeypatch, capsys, jpeg_bytes, mp4_bytes):
    # Regression: the extractor had its own copies of the matcher, parser and argument builder,
    # which missed these names, wrote no people tags and lost the hemisphere on videos.
    _zip(tmp_path, {
        ALBUM + "short.jpg": jpeg_bytes, ALBUM + "short.jpg.su.json": sidecar(timestamp=1000000000),
        ALBUM + "IMG_1.jpg": jpeg_bytes, ALBUM + "IMG_1.jpg.json": sidecar(timestamp=1100000000, people=["Person A"]),
        ALBUM + "IMG_1(1).jpg": jpeg_bytes, ALBUM + "IMG_1.jpg(1).json": sidecar(timestamp=1200000000),
        ALBUM + "IMG_1-bearbeitet.jpg": jpeg_bytes,
        ALBUM + "clip.mp4": mp4_bytes,
        ALBUM + "clip.mp4.supplemental-metadata.json": sidecar(timestamp=1300000000, lat=-12.5, lng=-34.25, description="two\nlines $5"),
    })
    output = _run(monkeypatch, capsys, tmp_path)
    assert "5 fixed, 0 no-metadata, 0 errors" in output
    out = tmp_path / "out"
    dates = {n: read_tags(out / n, "-ExifIFD:DateTimeOriginal").get("ExifIFD:DateTimeOriginal")
             for n in ("short.jpg", "IMG_1.jpg", "IMG_1(1).jpg", "IMG_1-bearbeitet.jpg")}
    assert dates == {
        "short.jpg": "2001:09:09 01:46:40", "IMG_1.jpg": "2004:11:09 11:33:20",
        "IMG_1(1).jpg": "2008:01:10 21:20:00", "IMG_1-bearbeitet.jpg": "2004:11:09 11:33:20",
    }
    assert read_tags(out / "IMG_1.jpg", "-XMP-iptcExt:PersonInImage")["XMP-iptcExt:PersonInImage"] == "Person A"
    clip = read_tags(out / "clip.mp4", "-Composite:GPSPosition", "-XMP-dc:Description", "-ImageDescription")
    assert [float(v) for v in clip["Composite:GPSPosition"].split()] == [-12.5, -34.25]


def test_exiftool_failure_is_reported_but_the_file_is_still_extracted(tmp_path, monkeypatch, capsys):
    _zip(tmp_path, {ALBUM + "broken.jpg": b"not a jpeg", ALBUM + "broken.jpg.supplemental-metadata.json": sidecar()})
    output = _run(monkeypatch, capsys, tmp_path)
    assert _files(tmp_path) == ["broken.jpg"]
    assert "0 fixed" in output and "1 metadata not written" in output and "Not a valid JPG" in output


def test_entry_names_cannot_escape_the_output_folder(tmp_path, monkeypatch, capsys, jpeg_bytes):
    _zip(tmp_path, {"../../escape1.jpg": jpeg_bytes, "/abs/escape2.jpg": jpeg_bytes, "Takeout/../../escape3.jpg": jpeg_bytes})
    _run(monkeypatch, capsys, tmp_path)
    assert _files(tmp_path) == ["escape1.jpg", "escape2.jpg", "escape3.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("escape")) == []


def test_same_name_in_two_albums_keeps_both_files(tmp_path, monkeypatch, capsys, jpeg_bytes):
    _zip(tmp_path, {"Takeout/Google Photos/A/IMG.jpg": jpeg_bytes, "Takeout/Google Photos/B/IMG.jpg": jpeg_bytes + b"\x00"})
    _run(monkeypatch, capsys, tmp_path)
    assert _files(tmp_path) == ["IMG.jpg", "IMG_1.jpg"]
    assert sorted(_progress(tmp_path)["processed_files"].values()) == ["IMG.jpg", "IMG_1.jpg"]


def test_nested_output_folder_is_created(tmp_path, monkeypatch, capsys, jpeg_bytes):
    _zip(tmp_path, {ALBUM + "a.jpg": jpeg_bytes})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path / "zips"), "--output", str(tmp_path / "new" / "nested" / "out")])
    extractor.main()
    assert (tmp_path / "new" / "nested" / "out" / "a.jpg").exists()


def test_help_still_works():
    result = subprocess.run([sys.executable, extractor.__file__, "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "--adb" in result.stdout
