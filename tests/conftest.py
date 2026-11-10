"""Shared pytest fixtures and the ExifTool gate.

Tests marked ``@pytest.mark.exiftool`` call the real ``exiftool`` binary. They are skipped
when it is not installed, unless ``GTMF_REQUIRE_EXIFTOOL=1`` is set (as in CI), in which
case a missing binary is an error instead of a silent skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.takeout_fixtures import make_jpeg_bytes, make_mp4_bytes, make_png_bytes

HAS_EXIFTOOL = shutil.which("exiftool") is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAS_EXIFTOOL:
        return
    if os.environ.get("GTMF_REQUIRE_EXIFTOOL") == "1":
        raise pytest.UsageError("GTMF_REQUIRE_EXIFTOOL=1 is set but exiftool is not on PATH")
    skip = pytest.mark.skip(reason="exiftool not installed")
    for item in items:
        if "exiftool" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def jpeg_bytes() -> bytes:
    return make_jpeg_bytes()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png_bytes()


@pytest.fixture
def mp4_bytes() -> bytes:
    return make_mp4_bytes()


def read_tags(path: Path | str, *tags: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Read tags from one file with ExifTool (numeric values, group names included)."""
    result = subprocess.run(
        ["exiftool", "-json", "-n", "-a", "-G1", *tags, str(path)],
        capture_output=True, text=True, timeout=60, check=True, env=env,
    )
    record: dict[str, Any] = json.loads(result.stdout)[0]
    record.pop("SourceFile", None)
    return record
