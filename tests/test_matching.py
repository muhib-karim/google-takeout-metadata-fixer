"""Tests for find_json_match: pairing media files with their Takeout JSON sidecars."""

import pytest

from google_takeout_metadata_fixer import find_json_match

D = "Takeout/Google Photos/Trip/"


@pytest.mark.parametrize(
    ("media", "names", "expected"),
    [
        # Rule 1: new supplemental-metadata format
        ("IMG_1.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        # Rule 2: duplicate index on the sidecar
        ("IMG_1.jpg", ["IMG_1.jpg.supplemental-metadata(1).json"], "IMG_1.jpg.supplemental-metadata(1).json"),
        # Rule 3: truncated suffix
        ("IMG_1.jpg", ["IMG_1.jpg.supplemental-metad.json"], "IMG_1.jpg.supplemental-metad.json"),
        ("IMG_1.jpg", ["IMG_1.jpg.supple.json"], "IMG_1.jpg.supple.json"),
        # Rule 3b: extreme truncation
        ("IMG_1.jpg", ["IMG_1.jpg.s.json"], "IMG_1.jpg.s.json"),
        ("IMG_1.jpg", ["IMG_1.jpg.su.json"], "IMG_1.jpg.su.json"),
        # Rule 4: Google duplicate media file photo(2).jpg
        ("IMG_1(2).jpg", ["IMG_1.jpg.supplemental-metadata(2).json"], "IMG_1.jpg.supplemental-metadata(2).json"),
        ("IMG_1(2).jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1(2).jpg", ["IMG_1.json"], "IMG_1.json"),
        # Rule 5: old formats
        ("IMG_1.jpg", ["IMG_1.jpg.json"], "IMG_1.jpg.json"),
        ("IMG_1.jpg", ["IMG_1.json"], "IMG_1.json"),
        ("IMG_1.jpg", ["IMG_1.jpg(1).json"], "IMG_1.jpg(1).json"),
        # Rule 6: edited variants in several languages
        ("IMG_1-edited.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1-bearbeitet.jpg", ["IMG_1.jpg.json"], "IMG_1.jpg.json"),
        ("IMG_1-modifié.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1-EDITED.jpg", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1-編集済み.jpg", ["IMG_1.jpg.supplemental-meta.json"], "IMG_1.jpg.supplemental-meta.json"),
        # Rule 7: live photo video picks up the still image's sidecar
        ("IMG_1.mp4", ["IMG_1.jpg.supplemental-metadata.json"], "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1.MOV", ["IMG_1.HEIC.json"], "IMG_1.HEIC.json"),
    ],
)
def test_filename_rules(media, names, expected):
    nameset = {D + n for n in names} | {D + media}
    assert find_json_match(nameset, D + media) == D + expected


def test_exact_match_preferred_over_old_format():
    nameset = {D + "a.jpg", D + "a.jpg.json", D + "a.jpg.supplemental-metadata.json"}
    assert find_json_match(nameset, D + "a.jpg") == D + "a.jpg.supplemental-metadata.json"


def test_does_not_cross_directories():
    nameset = {D + "a.jpg", "Takeout/Google Photos/Other/a.jpg.supplemental-metadata.json"}
    assert find_json_match(nameset, D + "a.jpg") is None


def test_media_at_zip_root():
    nameset = {"a.png", "a.png.supplemental-metadata.json"}
    assert find_json_match(nameset, "a.png") == "a.png.supplemental-metadata.json"


def test_title_index_fallback():
    nameset = {D + "weird_name.jpg", D + "something-else.json"}
    index = {"weird_name.jpg": D + "something-else.json"}
    assert find_json_match(nameset, D + "weird_name.jpg") is None
    assert find_json_match(nameset, D + "weird_name.jpg", index) == D + "something-else.json"


def test_no_match_returns_none():
    assert find_json_match({D + "x.jpg", D + "y.jpg.json"}, D + "x.jpg") is None


def test_live_photo_rule_only_for_videos():
    # A .png must not borrow a .jpg sidecar via the live-photo rule
    nameset = {D + "a.png", D + "a.jpg.supplemental-metadata.json"}
    assert find_json_match(nameset, D + "a.png") is None
