"""Shared pytest fixtures."""

import pytest

from tests.takeout_fixtures import make_jpeg_bytes


@pytest.fixture
def jpeg_bytes():
    pytest.importorskip("PIL")
    return make_jpeg_bytes()
