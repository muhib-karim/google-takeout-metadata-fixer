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


# ---------------------------------------------------------------------------
# Regression tests for the matcher rewrite. All names are synthetic.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import unicodedata  # noqa: E402

from google_takeout_metadata_fixer import SidecarIndex  # noqa: E402

SUFFIX = ".supplemental-metadata"


def _match(names, media, titles=None):
    """Match ``media`` inside directory D; ``titles`` maps sidecar name -> JSON title."""
    index = SidecarIndex([D + n for n in names], {D + k: v for k, v in (titles or {}).items()})
    found = index.match(D + media)
    return found[len(D):] if found else None


def test_old_format_duplicate_pairs_by_index():
    # Regression: IMG_1(1).jpg got the original's IMG_1.jpg.json instead of IMG_1.jpg(1).json
    names = ["IMG_1.jpg", "IMG_1.jpg.json", "IMG_1(1).jpg", "IMG_1.jpg(1).json"]
    assert _match(names, "IMG_1(1).jpg") == "IMG_1.jpg(1).json"
    assert _match(names, "IMG_1.jpg") == "IMG_1.jpg.json"
    assert _match(["IMG_1(1).jpg", "IMG_1.jpg(1).json"], "IMG_1(1).jpg") == "IMG_1.jpg(1).json"


def test_truncated_suffix_duplicates_pair_by_index():
    # Regression: both files got whichever sidecar the set happened to yield first
    base = "IMG_20190704_123456789_HDR"
    names = [f"{base}.jpg", f"{base}(1).jpg", f"{base}.jpg.supplemental-me.json", f"{base}.jpg.supplemental-me(1).json"]
    assert _match(names, f"{base}.jpg") == f"{base}.jpg.supplemental-me.json"
    assert _match(names, f"{base}(1).jpg") == f"{base}.jpg.supplemental-me(1).json"


def test_result_does_not_depend_on_the_hash_seed():
    script = (
        "from google_takeout_metadata_fixer import find_json_match as f;"
        "d='T/A/';b='IMG_20190704_123456789_HDR';"
        "ns={d+b+'.jpg',d+b+'(1).jpg',d+b+'.jpg.supplemental-me.json',d+b+'.jpg.supplemental-me(1).json',"
        "d+'x.jpg',d+'x(1).jpg',d+'x(2).jpg',d+'x.jpg.suppl(2).json',d+'x.jpg.suppl(1).json',d+'x.jpg.suppl.json'};"
        "print([f(ns,d+m) for m in (b+'.jpg',b+'(1).jpg','x.jpg','x(1).jpg','x(2).jpg')])"
    )
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outputs = set()
    for seed in ("0", "1", "2", "5", "42", "1234"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=repo)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout)
    assert len(outputs) == 1
    assert "supplemental-me(1).json" in outputs.pop()


@pytest.mark.parametrize("length", range(38, 62))
@pytest.mark.parametrize("cut", [46, 47])
def test_every_cut_position_of_the_sidecar_name(length, cut):
    # Takeout cuts "<media name>.supplemental-metadata" to a fixed length before adding ".json".
    # Regression: a cut right after the dot ("name.jpg..json") and any cut inside the media
    # name itself (names of 47+ characters) found nothing.
    media = ("family_reunion_summer_lake_house_" + "x" * 60)[: length - 4] + ".jpg"
    assert len(media) == length
    side = (media + SUFFIX)[:cut] + ".json"
    assert _match([media, side], media) == side


@pytest.mark.parametrize("length", [47, 48, 52, 60])
def test_truncated_long_name_with_duplicate_index(length):
    media = ("family_reunion_summer_lake_house_" + "x" * 60)[: length - 4] + ".jpg"
    copy = media[:-4] + "(1).jpg"
    names = [media, copy, media[:46] + ".json", media[:46] + "(1).json"]
    assert _match(names, media) == media[:46] + ".json"
    assert _match(names, copy) == media[:46] + "(1).json"


def test_truncation_counts_utf16_units():
    # 24 characters that take 46 UTF-16 units, followed by more name
    emoji = "\U0001F600" * 22 + "ab"
    media = emoji + "_rest_of_a_long_name.jpg"
    assert _match([media, emoji + ".json"], media) == emoji + ".json"
    cyrillic = "фото_из_отпуска_на_море_с_семьёй_и_друзьями_2019_года.jpg"
    assert _match([cyrillic, cyrillic[:46] + ".json"], cyrillic) == cyrillic[:46] + ".json"


def test_short_sidecar_is_not_taken_for_a_truncated_name():
    # "IMG_1.json" is a prefix of "IMG_10.jpg" but it is not a truncation of it
    assert _match(["IMG_10.jpg", "IMG_1.json"], "IMG_10.jpg") is None


def test_names_that_really_contain_an_index():
    names = ["photo (1).jpg", "photo (1).jpg.supplemental-metadata.json", "Scan(2).jpg", "Scan(2).jpg.json"]
    assert _match(names, "photo (1).jpg") == "photo (1).jpg.supplemental-metadata.json"
    assert _match(names, "Scan(2).jpg") == "Scan(2).jpg.json"
    assert _match(["IMG(1)(2).jpg", "IMG(1).jpg.supplemental-metadata(2).json"], "IMG(1)(2).jpg") == (
        "IMG(1).jpg.supplemental-metadata(2).json"
    )


def test_shared_truncated_prefix_is_decided_by_title_or_not_at_all():
    prefix = "family_reunion_summer_lake_house_2019_group_ph"
    assert len(prefix) == 46
    a, b = prefix + "oto_A.jpg", prefix + "oto_B.jpg"
    names = [a, b, prefix + ".json", prefix + "(1).json"]
    # Without titles nothing says which file each sidecar belongs to: better none than wrong
    assert _match(names, a) is None and _match(names, b) is None
    titles = {prefix + ".json": b, prefix + "(1).json": a}
    assert _match(names, a, titles) == prefix + "(1).json"
    assert _match(names, b, titles) == prefix + ".json"


def test_edited_copies_and_live_videos_of_duplicates():
    # Regression: all of these returned None
    names = [
        "IMG_2.jpg", "IMG_2(1).jpg", "IMG_2-edited.jpg", "IMG_2-edited(1).jpg", "IMG_2(1)-edited.jpg",
        "IMG_2.jpg.supplemental-metadata.json", "IMG_2.jpg.supplemental-metadata(1).json",
        "IMG_3.HEIC", "IMG_3.MP4", "IMG_3(1).HEIC", "IMG_3(1).MP4",
        "IMG_3.HEIC.supplemental-metadata.json", "IMG_3.HEIC.supplemental-metadata(1).json",
    ]
    assert _match(names, "IMG_2-edited.jpg") == "IMG_2.jpg.supplemental-metadata.json"
    assert _match(names, "IMG_2-edited(1).jpg") == "IMG_2.jpg.supplemental-metadata(1).json"
    assert _match(names, "IMG_2(1)-edited.jpg") == "IMG_2.jpg.supplemental-metadata(1).json"
    assert _match(names, "IMG_3.MP4") == "IMG_3.HEIC.supplemental-metadata.json"
    assert _match(names, "IMG_3(1).MP4") == "IMG_3.HEIC.supplemental-metadata(1).json"


def test_edited_suffix_in_decomposed_unicode():
    # File names extracted on macOS are often NFD: "é" is "e" plus a combining accent
    media = unicodedata.normalize("NFD", "IMG_1-modifié.jpg")
    assert media != "IMG_1-modifié.jpg"
    assert _match([media, "IMG_1.jpg.supplemental-metadata.json"], media) == "IMG_1.jpg.supplemental-metadata.json"


def test_a_sidecar_is_not_borrowed_while_its_owner_is_present():
    # Regression: IMG_1.jpg took the sidecar of IMG_1(1).jpg
    names = ["IMG_1.jpg", "IMG_1(1).jpg", "IMG_1.jpg.supplemental-metadata(1).json"]
    assert _match(names, "IMG_1.jpg") is None
    assert _match(names, "IMG_1(1).jpg") == "IMG_1.jpg.supplemental-metadata(1).json"
    names = ["IMG_1.jpg", "IMG_1(2).jpg", "IMG_1.jpg.supplemental-metadata.json"]
    assert _match(names, "IMG_1(2).jpg") is None


def test_sidecar_named_after_the_stem_in_the_new_format():
    assert _match(["20030616.jpg", "20030616.supplemental-metadata.json"], "20030616.jpg") == (
        "20030616.supplemental-metadata.json"
    )


def test_title_fallback_stays_in_the_directory():
    # Regression: the title index returned a sidecar from another album
    other = "Takeout/Google Photos/Other/whatever.json"
    nameset = {D + "scan.jpg", other}
    assert find_json_match(nameset, D + "scan.jpg", {"scan.jpg": other}) is None
    index = SidecarIndex([D + "scan.jpg", D + "local.json", other], {other: "scan.jpg", D + "local.json": "scan.jpg"})
    assert index.match(D + "scan.jpg") == D + "local.json"


def test_find_json_match_accepts_an_index():
    index = SidecarIndex([D + "a.jpg", D + "a.jpg.supplemental-metadata.json"])
    assert find_json_match(index, D + "a.jpg") == D + "a.jpg.supplemental-metadata.json"
    assert D + "a.jpg" in index and D + "missing.jpg" not in index


def test_matching_a_large_old_format_listing_is_fast():
    # Regression: every lookup scanned the whole listing (about 180 s for this input)
    names = []
    for i in range(20000):
        names += [f"{D}IMG_{i:06d}.jpg", f"{D}IMG_{i:06d}.jpg.json"]
    nameset = set(names)
    started = time.perf_counter()
    for i in range(20000):
        assert find_json_match(nameset, f"{D}IMG_{i:06d}.jpg") == f"{D}IMG_{i:06d}.jpg.json"
    assert time.perf_counter() - started < 20
