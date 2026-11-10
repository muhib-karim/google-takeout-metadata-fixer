"""GPS round-trips through the real ExifTool: hemisphere signs and altitude reference.

The coordinates are arbitrary synthetic points, one per hemisphere combination.
"""

from __future__ import annotations

import pytest

import google_takeout_metadata_fixer as fixer
from tests.conftest import read_tags
from tests.takeout_fixtures import make_jpeg_bytes, make_mp4_bytes, make_png_bytes

CONTAINERS = {"photo.jpg": make_jpeg_bytes, "image.png": make_png_bytes, "clip.mp4": make_mp4_bytes}
POINTS = [(12.5, 34.25), (-12.5, 34.25), (12.5, -34.25), (-12.5, -34.25), (0.0, -34.25), (-12.5, 0.0)]


def _write(tmp_path, name, geo):
    target = tmp_path / name
    target.write_bytes(CONTAINERS[name]())
    metadata = fixer.parse_takeout_json({"geoData": geo})
    assert fixer.run_exiftool_batch([(target, fixer.build_exiftool_args(metadata, target))]) == (1, 0)
    return target


@pytest.mark.exiftool
@pytest.mark.parametrize("name", sorted(CONTAINERS))
@pytest.mark.parametrize(("lat", "lng"), POINTS)
def test_hemisphere_survives_round_trip(tmp_path, name, lat, lng):
    # Regression: MP4/MOV (XMP-only GPS) lost the sign, so southern and western
    # positions were written as northern and eastern ones.
    target = _write(tmp_path, name, {"latitude": lat, "longitude": lng, "altitude": 0})
    position = read_tags(target, "-Composite:GPSPosition")["Composite:GPSPosition"]
    assert [float(v) for v in position.split()] == pytest.approx([lat, lng], abs=1e-6)


@pytest.mark.exiftool
@pytest.mark.parametrize("name", sorted(CONTAINERS))
@pytest.mark.parametrize("altitude", [-420.5, 35.0])
def test_altitude_sign_survives_round_trip(tmp_path, name, altitude):
    # Regression: "-GPSAltitudeRef=1" was stored as "above sea level", turning -420 m into +420 m.
    target = _write(tmp_path, name, {"latitude": 12.5, "longitude": 34.25, "altitude": altitude})
    assert read_tags(target, "-Composite:GPSAltitude")["Composite:GPSAltitude"] == pytest.approx(altitude)


def test_coordinates_are_never_written_in_exponent_notation():
    args = fixer.build_exiftool_args({"latitude": 0.00001234, "longitude": -0.00005, "altitude": 0}, "a.jpg")
    assert "-GPSLatitude=0.00001234" in args
    assert "-GPSLongitude=-0.00005" in args
