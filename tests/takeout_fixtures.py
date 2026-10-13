"""Helpers for building tiny synthetic Google Takeout fixtures (no real photos needed)."""

import io
import json
import zipfile


def make_jpeg_bytes(color=(200, 80, 40), size=(8, 8)):
    """Return the bytes of a tiny solid-colour JPEG with no EXIF data."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def sidecar(timestamp=1600000000, lat=0.0, lng=0.0, alt=0.0, description="", people=(), title=None):
    """Build a dict shaped like a Google Takeout JSON sidecar."""
    data = {
        "title": title,
        "description": description,
        "photoTakenTime": {"timestamp": str(timestamp), "formatted": ""},
        "geoData": {"latitude": lat, "longitude": lng, "altitude": alt},
        "people": [{"name": n} for n in people],
    }
    if title is None:
        del data["title"]
    return data


def write_zip(path, members):
    """Write a zip at ``path`` from a {name: bytes|dict} mapping (dicts are JSON-encoded)."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            if isinstance(payload, dict):
                payload = json.dumps(payload).encode("utf-8")
            zf.writestr(name, payload)
    return path
