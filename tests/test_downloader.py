"""Tests for the optional Google Photos downloader (network calls are replaced)."""

from __future__ import annotations

import os
import stat
import sys

import pytest

pytest.importorskip("requests")
pytest.importorskip("google_auth_oauthlib")

import google_photos_downloader as downloader  # noqa: E402


class _Response:
    content = b"synthetic image bytes"

    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: _Response())


@pytest.mark.parametrize("name", ["../outside.jpg", "../../etc/outside.jpg", "/abs/outside.jpg", "..\\..\\outside.jpg", "a/b/outside.jpg"])
def test_api_file_name_cannot_leave_the_download_directory(tmp_path, no_network, name):
    # Regression: "../outside.jpg" was written next to the download directory
    photos = tmp_path / "photos"
    photos.mkdir()
    assert downloader.download_item({"filename": name, "baseUrl": "https://example.invalid/x"}, photos) is True
    assert sorted(p.name for p in photos.iterdir()) == ["outside.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photos"]


@pytest.mark.parametrize("name", ["", "..", ".", "dir/"])
def test_unusable_api_file_name_is_rejected(tmp_path, no_network, name):
    with pytest.raises(ValueError):
        downloader.download_item({"filename": name, "baseUrl": "https://example.invalid/x"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_token_file_is_private(tmp_path, monkeypatch):
    # Regression: token.json (an OAuth refresh token) was created world-readable
    monkeypatch.chdir(tmp_path)
    old_umask = os.umask(0o022)
    try:

        class Creds:
            def to_json(self) -> str:
                return '{"refresh_token": "synthetic"}'

        downloader.save_token(Creds())
        assert stat.S_IMODE(os.stat(downloader.TOKEN_FILE).st_mode) == 0o600
        os.chmod(downloader.TOKEN_FILE, 0o644)  # a file left behind by an earlier version
        downloader.save_token(Creds())
        assert stat.S_IMODE(os.stat(downloader.TOKEN_FILE).st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_leftover_part_file_does_not_skip_download(tmp_path, no_network):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / ".clip.mp4.part").write_bytes(b"truncated")
    assert downloader.download_item({"filename": "clip.mp4", "baseUrl": "https://example.invalid/v"}, photos) is True
    assert (photos / "clip.mp4").read_bytes() == b"synthetic image bytes"
    assert not (photos / ".clip.mp4.part").exists()


def test_persistent_401_raises_instead_of_looping_forever(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Creds:
        token = "expired"
        refreshes = 0

        def refresh(self, _req):
            self.refreshes += 1

        def to_json(self):
            return '{"refresh_token": "x"}'

    creds = Creds()
    monkeypatch.setattr(downloader, "authenticate", lambda: creds)

    def always_401(*_a, **_kw):
        resp = downloader.requests.Response()
        resp.status_code = 401
        raise downloader.requests.exceptions.HTTPError(response=resp)

    monkeypatch.setattr(downloader, "list_media_items", always_401)
    with pytest.raises(downloader.requests.exceptions.HTTPError):
        downloader.main()
    assert creds.refreshes == 1
