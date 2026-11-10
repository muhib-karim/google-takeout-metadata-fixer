"""CLI smoke tests and an end-to-end run of the local-zip mode."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import google_takeout_metadata_fixer as fixer
from tests.takeout_fixtures import sidecar, write_zip

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("google_takeout_metadata_fixer.py", "--local-zips"),
        ("extract_takeout_media.py", "--adb"),
        ("adb_resumable_copy.py", "--dest-dir"),
        ("recover_takeout_zip_metadata.py", "--remote-zip"),
    ],
)
def test_help(script, expected):
    r = subprocess.run(
        [sys.executable, str(REPO / script), "--help"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 0, r.stderr
    assert expected in r.stdout


def test_recover_requires_remote_zip():
    r = subprocess.run(
        [sys.executable, str(REPO / "recover_takeout_zip_metadata.py")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 2
    assert "--remote-zip" in r.stderr


def test_missing_output_dir_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--output", str(tmp_path / "nope"), "--local-zips", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        fixer.main()
    assert exc.value.code == 1
    assert "Output directory not found" in capsys.readouterr().out


def _build_takeout(tmp_path, jpeg_bytes):
    """Create a Takeout zip plus an 'already extracted' output folder."""
    zips = tmp_path / "zips"
    out = tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    album = "Takeout/Google Photos/Holiday/"
    write_zip(
        zips / "takeout-001.zip",
        {
            album + "IMG_1.jpg": jpeg_bytes,
            album + "IMG_1.jpg.supplemental-metadata.json": sidecar(
                timestamp=1600000000, lat=48.8584, lng=2.2945,
                description="Brace {test}", people=["Alice"], title="IMG_1.jpg",
            ),
            album + "IMG_2(1).jpg": jpeg_bytes,
            album + "IMG_2.jpg.supplemental-metadata(1).json": sidecar(timestamp=1500000000, title="IMG_2.jpg"),
            album + "orphan.jpg": jpeg_bytes,
        },
    )
    for name in ("IMG_1.jpg", "IMG_2(1).jpg", "orphan.jpg"):
        (out / name).write_bytes(jpeg_bytes)
    return zips, out


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    fixer.main()


@pytest.mark.exiftool
def test_dry_run_does_not_modify_files_or_save_progress(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = _build_takeout(tmp_path, jpeg_bytes)
    monkeypatch.chdir(tmp_path)
    before = (out / "IMG_1.jpg").read_bytes()

    _run_main(monkeypatch, ["--output", str(out), "--local-zips", str(zips), "--dry-run"])

    output = capsys.readouterr().out
    assert "fixed=2 no_json=1" in output
    assert "[DRY RUN] No files were modified." in output
    assert (out / "IMG_1.jpg").read_bytes() == before
    # Regression: a dry run used to persist progress, making the real run skip everything
    assert not (tmp_path / fixer.PROGRESS_FILE).exists()


@pytest.mark.exiftool
def test_end_to_end_writes_exif_and_resumes(tmp_path, monkeypatch, capsys, jpeg_bytes):
    zips, out = _build_takeout(tmp_path, jpeg_bytes)
    monkeypatch.chdir(tmp_path)

    _run_main(monkeypatch, ["--output", str(out), "--local-zips", str(zips)])
    assert "fixed=2 no_json=1" in capsys.readouterr().out

    r = subprocess.run(
        ["exiftool", "-json", "-n", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude",
         "-ImageDescription", "-XMP:PersonInImage", str(out / "IMG_1.jpg"), str(out / "IMG_2(1).jpg")],
        capture_output=True, text=True, timeout=60, check=True,
    )
    img1, img2 = json.loads(r.stdout)
    assert img1["DateTimeOriginal"] == "2020:09:13 12:26:40"
    assert img1["GPSLatitude"] == pytest.approx(48.8584, abs=1e-4)
    assert img1["GPSLongitude"] == pytest.approx(2.2945, abs=1e-4)
    assert img1["ImageDescription"] == "Brace {test}"
    assert img1["PersonInImage"] == "Alice"
    assert img2["DateTimeOriginal"] == "2017:07:14 02:40:00"

    progress = json.loads((tmp_path / fixer.PROGRESS_FILE).read_text())
    assert progress["completed_zips"] == ["takeout-001.zip"]

    # A second run resumes from the progress file and skips the finished zip
    _run_main(monkeypatch, ["--output", str(out), "--local-zips", str(zips)])
    assert "SKIP (already done): takeout-001.zip" in capsys.readouterr().out


@pytest.mark.exiftool
def test_duplicates_and_long_names_get_their_own_sidecar_end_to_end(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Old-format duplicate naming, a truncated sidecar name that needs its title, and a
    # same-titled sidecar in another album that must not be used.
    zips, out = tmp_path / "zips", tmp_path / "out"
    zips.mkdir()
    out.mkdir()
    album = "Takeout/Google Photos/Album/"
    prefix = "family_reunion_summer_lake_house_2019_group_ph"  # 46 characters
    long_a, long_b = prefix + "oto_A.jpg", prefix + "oto_B.jpg"
    members = {
        album + "IMG_1.jpg": jpeg_bytes,
        album + "IMG_1.jpg.json": sidecar(timestamp=1000000000, title="IMG_1.jpg"),
        album + "IMG_1(1).jpg": jpeg_bytes,
        album + "IMG_1.jpg(1).json": sidecar(timestamp=1100000000, title="IMG_1.jpg"),
        album + long_a: jpeg_bytes,
        album + long_b: jpeg_bytes,
        album + prefix + ".json": sidecar(timestamp=1200000000, title=long_b),
        album + prefix + "(1).json": sidecar(timestamp=1300000000, title=long_a),
        album + "lonely.jpg": jpeg_bytes,
        "Takeout/Google Photos/Other/unrelated.json": sidecar(timestamp=1400000000, title="lonely.jpg"),
    }
    write_zip(zips / "takeout-001.zip", members)
    for name in ("IMG_1.jpg", "IMG_1(1).jpg", long_a, long_b, "lonely.jpg"):
        (out / name).write_bytes(jpeg_bytes)
    monkeypatch.chdir(tmp_path)

    _run_main(monkeypatch, ["--output", str(out), "--local-zips", str(zips)])

    assert "fixed=4 no_json=1" in capsys.readouterr().out
    r = subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", *[str(out / n) for n in ("IMG_1.jpg", "IMG_1(1).jpg", long_a, long_b, "lonely.jpg")]],
        capture_output=True, text=True, timeout=60, check=True,
    )
    dates = [d.get("DateTimeOriginal") for d in json.loads(r.stdout)]
    assert dates == [
        "2001:09:09 01:46:40",  # IMG_1.jpg      <- IMG_1.jpg.json
        "2004:11:09 11:33:20",  # IMG_1(1).jpg   <- IMG_1.jpg(1).json
        "2011:03:13 07:06:40",  # ..._A.jpg      <- the "(1)" sidecar, by title
        "2008:01:10 21:20:00",  # ..._B.jpg      <- the plain sidecar, by title
        None,                   # lonely.jpg: its only "match" is in another album
    ]
