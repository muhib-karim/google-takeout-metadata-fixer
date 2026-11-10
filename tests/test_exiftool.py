"""Tests for the ExifTool invocation layer: argument escaping, paths and per-file results."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import google_takeout_metadata_fixer as fixer
from tests.conftest import read_tags

# --- pure helpers (no exiftool needed) -------------------------------------------------


def test_argfile_line_escapes_control_characters():
    line = fixer._argfile_line("-ImageDescription=one\ntwo\\three\tfour\r\x00")
    assert line == "#[CSTR]-ImageDescription=one\\ntwo\\\\three\\tfour\\r"
    assert "\n" not in line


def test_parse_output_uses_exit_status_markers():
    stdout = "    1 image files updated\n{gtmf:0:0}\n    0 image files updated\n{gtmf:1:1}\n"
    stderr = "{gtmf:0}\nError: Not a valid JPG - /x/b.jpg\n{gtmf:1}\n"
    results = fixer._parse_exiftool_output(stdout, stderr, 3)
    assert results[0] == fixer.ExifResult(True, "")
    assert results[1] is not None and not results[1].ok and "Not a valid JPG" in results[1].message
    assert results[2] is None  # no marker: exiftool never reached that file


def test_parse_output_without_status_support_falls_back_to_summary_lines():
    # Older ExifTool releases print the marker text literally instead of the exit status
    stdout = (
        "    1 image files updated\n{gtmf:0:${status}}\n"
        "    0 image files updated\n    1 files weren't updated due to errors\n{gtmf:1:${status}}\n"
    )
    results = fixer._parse_exiftool_output(stdout, "{gtmf:0}\nError: boom\n{gtmf:1}\n", 2)
    assert [r.ok for r in results if r is not None] == [True, False]


def test_missing_exiftool_gives_no_results(monkeypatch, tmp_path, jpeg_bytes):
    (tmp_path / "a.jpg").write_bytes(jpeg_bytes)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    items = [(tmp_path / "a.jpg", ["-Artist=x"]), (tmp_path / "a.jpg", ["-Artist=y"])]
    assert fixer.run_exiftool_batch_detailed(items) == [None, None]
    assert fixer.run_exiftool_batch(items) == (0, 2)


def test_empty_batch():
    assert fixer.run_exiftool_batch([]) == (0, 0)
    assert fixer.run_exiftool_batch_detailed([]) == []


# --- behaviour against the real binary --------------------------------------------------


@pytest.mark.exiftool
def test_multiline_description_is_written_verbatim(tmp_path, jpeg_bytes):
    # Regression: the argfile is one argument per line, so line 2 of a caption used to be
    # read as a separate argument and the caption was cut at the first line break.
    target = tmp_path / "a.jpg"
    target.write_bytes(jpeg_bytes)
    caption = 'Day one\nline two \\ with "quotes"\tand a tab'
    args = fixer.build_exiftool_args({"description": caption}, target)

    assert fixer.run_exiftool_batch([(target, args)]) == (1, 0)
    assert read_tags(target, "-ImageDescription")["IFD0:ImageDescription"] == caption


@pytest.mark.exiftool
def test_caption_lines_cannot_inject_arguments_or_touch_other_files(tmp_path, jpeg_bytes):
    # Regression: a caption line naming another file made ExifTool process that file too,
    # and a line starting with "-" was executed as an option.
    target, bystander = tmp_path / "a.jpg", tmp_path / "bystander.jpg"
    target.write_bytes(jpeg_bytes)
    bystander.write_bytes(jpeg_bytes)
    assert fixer.run_exiftool_batch([(bystander, ["-Artist=keep-me"])]) == (1, 0)
    before = bystander.read_bytes()

    caption = f"caption\n{bystander}\n-Artist=\n#[CSTR]-Copyright=injected\n-execute"
    metadata = {"description": caption, "people": ["Sam\n-Artist=injected"], "datetime": "2020:09:13 12:26:40"}
    assert fixer.run_exiftool_batch([(target, fixer.build_exiftool_args(metadata, target))]) == (1, 0)

    assert bystander.read_bytes() == before
    tags = read_tags(target, "-ImageDescription", "-Artist", "-Copyright", "-PersonInImage")
    assert tags["IFD0:ImageDescription"] == caption
    assert tags["XMP-iptcExt:PersonInImage"] == "Sam\n-Artist=injected"
    assert "IFD0:Artist" not in tags and "IFD0:Copyright" not in tags


@pytest.mark.exiftool
@pytest.mark.parametrize("name", ["-leading-dash.jpg", "#hash.jpg", " leading-space.jpg", "naïve ünïcode 写真.jpg"])
def test_awkward_relative_file_names_are_written(tmp_path, monkeypatch, jpeg_bytes, name):
    # Regression: with a relative path these were read as an option, a comment and a
    # different file name respectively, and nothing was written.
    monkeypatch.chdir(tmp_path)
    Path(name).write_bytes(jpeg_bytes)

    assert fixer.run_exiftool_batch([(Path(name), ["-DateTimeOriginal=2020:01:01 00:00:00"])]) == (1, 0)
    assert read_tags(tmp_path / name, "-DateTimeOriginal")["ExifIFD:DateTimeOriginal"] == "2020:01:01 00:00:00"


@pytest.mark.exiftool
@pytest.mark.skipif(sys.platform == "win32", reason="line breaks are not valid in Windows file names")
def test_file_name_containing_a_line_break(tmp_path, jpeg_bytes):
    target = tmp_path / "two\nlines.jpg"
    target.write_bytes(jpeg_bytes)
    assert fixer.run_exiftool_batch([(target, ["-Artist=x"])]) == (1, 0)


@pytest.mark.exiftool
def test_results_are_reported_per_file(tmp_path, jpeg_bytes):
    good1, junk, missing, good2 = (tmp_path / n for n in ("good1.jpg", "junk.jpg", "missing.jpg", "good2.jpg"))
    good1.write_bytes(jpeg_bytes)
    good2.write_bytes(jpeg_bytes)
    junk.write_bytes(b"this is not a jpeg")
    items = [(p, ["-Artist=x"]) for p in (good1, junk, missing, good2)]

    results = fixer.run_exiftool_batch_detailed(items)

    assert [r is not None and r.ok for r in results] == [True, False, False, True]
    assert results[1] is not None and "Not a valid JPG" in results[1].message
    assert results[2] is not None and "not found" in results[2].message.lower()
    assert fixer.run_exiftool_batch(items) == (2, 2)


@pytest.mark.exiftool
def test_no_temporary_argfile_is_left_behind(tmp_path, monkeypatch, jpeg_bytes):
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    target = tmp_path / "a.jpg"
    target.write_bytes(jpeg_bytes)
    assert fixer.run_exiftool_batch([(target, ["-Artist=x"])]) == (1, 0)
    assert [p for p in os.listdir(scratch) if p.endswith(".args")] == []
