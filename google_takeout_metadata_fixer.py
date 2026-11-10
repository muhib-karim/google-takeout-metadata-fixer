#!/usr/bin/env python3
"""
Google Takeout Metadata Fixer
=============================
Restores metadata from Google Takeout (Google Photos) exports.

Embeds dates, GPS coordinates, descriptions, and people tags from the JSON sidecars
directly into your photos and videos using ExifTool, so they sort correctly everywhere.

Two modes:
  ADB mode:   Streams JSON sidecars from zips on your Android phone via USB.
              Uses virtually ZERO extra disk space.
  Local mode: Reads JSON sidecars from local zip files on your computer.

Features:
  - Handles Google's new 'supplemental-metadata' JSON naming format
  - Handles truncated suffixes (e.g. .supple.json, .supplemental-metad.json)
  - Handles duplicate files: photo(2).jpg -> photo.jpg.supplemental-metadata(2).json
  - Handles -edited files in 12+ languages (English, German, French, etc.)
  - Handles live photos: video.mp4 -> photo.jpg's JSON
  - Falls back to JSON 'title' field matching when filename matching fails
  - Falls back to 'creationTime' when 'photoTakenTime' is missing
  - Writes: DateTimeOriginal, CreateDate, ModifyDate, GPS coordinates,
    descriptions, people tags, and video-specific tags
  - Batched ExifTool calls for speed (~50 files per invocation)
  - Resume support — safely restarts from where it left off

Requirements:
  - Python 3.10+
  - ExifTool (https://exiftool.org) on PATH
  - ADB on PATH (only for ADB mode — phone connected via USB)

Usage:
  # ADB mode (zips on phone):
    python google_takeout_metadata_fixer.py --output ./Output --phone-dir /sdcard/Download

  # Local mode (zips on computer):
    python google_takeout_metadata_fixer.py --output ./Output --local-zips ./takeout-zips/

  # Dry run (preview without modifying files):
    python google_takeout_metadata_fixer.py --output ./Output --local-zips ./zips/ --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm"}

EXIFTOOL_BATCH_SIZE = 50   # files per exiftool invocation
PROGRESS_FILE = "fix_metadata_progress.json"

# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------

def is_media(name):
    return Path(name).suffix.lower() in MEDIA_EXTENSIONS


def format_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def format_time(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def _fresh_progress() -> dict[str, Any]:
    return {"completed_zips": [], "processed_entries": []}


def load_progress(quarantine: bool = True) -> dict[str, Any]:
    """Load the progress file from the current directory.

    A file that cannot be read or parsed (a write cut short by a crash, for example) is
    moved aside as ``<name>.corrupt-<time>`` and a fresh state is returned, so a damaged
    file costs a re-run instead of a traceback. With ``quarantine=False`` the damaged file
    is left where it is.
    """
    if not os.path.exists(PROGRESS_FILE):
        return _fresh_progress()
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("progress file does not hold a JSON object")
    except (OSError, ValueError) as exc:
        print(f"  WARNING: {PROGRESS_FILE} could not be read ({exc}).")
        if quarantine:
            aside = f"{PROGRESS_FILE}.corrupt-{int(time.time())}"
            try:
                os.replace(PROGRESS_FILE, aside)
                print(f"           It was moved to {aside}; starting with empty progress.")
            except OSError:
                print("           Starting with empty progress.")
        return _fresh_progress()
    return data


def save_progress(progress: dict[str, Any]) -> None:
    for _attempt in range(5):
        try:
            tmp = PROGRESS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(progress, f)
            os.replace(tmp, PROGRESS_FILE)
            return
        except PermissionError:
            time.sleep(1)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f)


class ProgressStore:
    """Resume state for a run: what happened to each zip entry, and which zips are finished.

    Every zip entry ends in one of three states:

    * processed: it has a final outcome (written, no sidecar, nothing to write).
      It is never looked at again.
    * failed:    ExifTool reported an error for its file. It is remembered with the message
                 and retried only when asked to (``--retry-failed``).
    * neither:   no result was obtained (timeout, crash, interrupt). It is retried on the
                 next run, and its zip is not marked completed.

    The state lives in the JSON progress file. Rewriting that file after every batch would
    mean megabytes per write on a large library, so outcomes are appended to a journal next
    to it as they happen and folded into the JSON file by :meth:`checkpoint`. A run that is
    killed outright is recovered from the journal.
    """

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.journal_path = PROGRESS_FILE + ".journal"
        data = load_progress(quarantine=not dry_run)
        self.extra = {k: v for k, v in data.items()
                      if k not in ("completed_zips", "processed_entries", "failed_entries", "targets")}
        self.completed_zips: list[str] = [z for z in _as_list(data.get("completed_zips")) if isinstance(z, str)]
        self.processed: set[str] = {e for e in _as_list(data.get("processed_entries")) if isinstance(e, str)}
        self.failed: dict[str, str] = {
            str(k): str(v) for k, v in _as_dict(data.get("failed_entries")).items()
        }
        self.targets: dict[str, str] = {
            str(k): v for k, v in _as_dict(data.get("targets")).items() if isinstance(v, str)
        }
        self._journal: Any = None
        self._replay_journal()

    def _replay_journal(self) -> None:
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
                self._apply(record["e"], record["s"], record.get("m", ""), record.get("t"))
            except (ValueError, KeyError, TypeError):
                continue  # a line cut short by the crash that left the journal behind

    def _apply(self, entry_key: str, status: str, message: str, target: str | None) -> None:
        if status == "done":
            self.processed.add(entry_key)
            self.failed.pop(entry_key, None)
        elif status == "failed":
            self.processed.discard(entry_key)
            self.failed[entry_key] = message
        if isinstance(target, str):
            self.targets[entry_key] = target

    def record(self, entry_key: str, status: str, *, message: str = "", target: str | None = None) -> None:
        """Record the outcome of one entry: status is "done" or "failed"."""
        self._apply(entry_key, status, message, target)
        if self.dry_run:
            return
        if self._journal is None:
            self._journal = open(self.journal_path, "a", encoding="utf-8")  # noqa: SIM115
        record = {"e": entry_key, "s": status}
        if message:
            record["m"] = message
        if target:
            record["t"] = target
        self._journal.write(json.dumps(record) + "\n")
        self._journal.flush()

    def has_failed(self, zip_name: str) -> bool:
        prefix = f"{zip_name}::"
        return any(key.startswith(prefix) for key in self.failed)

    def complete_zip(self, zip_name: str) -> None:
        if zip_name not in self.completed_zips:
            self.completed_zips.append(zip_name)

    def checkpoint(self) -> None:
        """Fold everything recorded so far into the progress file and drop the journal."""
        if self.dry_run:
            return
        save_progress({
            **self.extra,
            "completed_zips": self.completed_zips,
            "processed_entries": sorted(self.processed),
            "failed_entries": self.failed,
            "targets": self.targets,
        })
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        try:
            os.remove(self.journal_path)
        except OSError:
            pass


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# JSON SIDECAR MATCHING (the core challenge)
#
# Google Takeout names the JSON sidecar of a media file inconsistently:
#   - photo.jpg.supplemental-metadata.json          new format
#   - photo.jpg.supplemental-metad.json             new format, cut short
#   - photo.jpg.json / photo.json                   old format (full name / stem)
#   - photo(1).jpg -> photo.jpg.supplemental-metadata(1).json   duplicate, new format
#   - photo(1).jpg -> photo.jpg(1).json                         duplicate, old format
#   - photo-edited.jpg -> photo.jpg's sidecar       edited copies
#   - video.mp4 -> photo.heic's sidecar             live photos
#
# The sidecar name without ".json" is cut to 46 UTF-16 units, so the cut can land
# anywhere: inside ".supplemental-metadata", right after its dot, or, for long media
# names, inside the media name itself.
#
# Matching works per directory. Every media file in a directory is resolved once:
# strict matches first (the duplicate index of the file and of the sidecar must agree),
# then, for files still without a sidecar, a sidecar nobody claimed may be borrowed.
# That order is what keeps two files from being handed the same sidecar.
# ---------------------------------------------------------------------------

SUPPLEMENTAL = "supplemental-metadata"
TRUNCATED_LENGTHS = (46, 47)  # UTF-16 units of a sidecar base name that was cut short
STILL_EXTENSIONS = (".jpg", ".jpeg", ".heic", ".png", ".JPG", ".JPEG", ".HEIC", ".PNG")
LIVE_VIDEO_EXTENSIONS = (".mp4", ".mov")

_INDEX = re.compile(r"\((\d+)\)")
_EDITED_STEM = re.compile(
    r"^(.+)-(?:edited|bearbeitet|modifi[eé]|editado|modificato|bewerkt|"
    r"edytowany|redigerad|muokattu|d[uü]zenlendi|отредактировано|編集済み)$",
    re.IGNORECASE,
)

# Match tiers, best first
_FULL, _CUT, _PLAIN, _STEM_OLD, _STEM_NEW, _TRUNCATED = range(6)


def _split_entry(entry: str) -> tuple[str, str]:
    """Split a zip entry into (directory including the trailing slash, base name)."""
    head, sep, tail = entry.rpartition("/")
    return head + sep, tail


def _strip_last_index(name: str) -> tuple[str, int | None]:
    """Remove the last "(N)" from a name. Returns (name without it, N or None)."""
    matches = list(_INDEX.finditer(name))
    if not matches:
        return name, None
    last = matches[-1]
    return name[:last.start()] + name[last.end():], int(last.group(1))


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_cut(text: str, units: int) -> str:
    """Longest prefix of text that fits in the given number of UTF-16 units."""
    used = 0
    for position, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > units:
            return text[:position]
    return text


class _Directory:
    """The media files and sidecars of one zip directory."""

    def __init__(self) -> None:
        self.media: set[str] = set()
        self.titles: dict[str, str] = {}
        # (name the sidecar is for, duplicate index) -> [(tier, tie-break, sidecar base name)]
        self.by_target: dict[tuple[str, int | None], list[tuple[int, int, str]]] = {}
        self.resolved: dict[str, str | None] | None = None

    def add_sidecar(self, basename: str) -> None:
        base = basename[: -len(".json")]
        readings: list[tuple[str, int | None]] = [(base, None)]
        stripped, index = _strip_last_index(base)
        if index is not None:
            readings.append((stripped, index))
        for name, idx in readings:
            self._add(name, idx, _PLAIN, 0, basename)
            head, dot, tail = name.rpartition(".")
            if dot and tail == SUPPLEMENTAL:
                self._add(head, idx, _FULL, 0, basename)
            elif dot and SUPPLEMENTAL.startswith(tail):
                # tail is a cut of "supplemental-metadata", possibly empty ("name.jpg..json")
                self._add(head, idx, _CUT, -len(tail), basename)

    def _add(self, target: str, index: int | None, tier: int, tie: int, basename: str) -> None:
        self.by_target.setdefault((target, index), []).append((tier, tie, basename))


class SidecarIndex:
    """Index of one zip listing for matching media files to their JSON sidecars.

    names:  every entry name in the zip
    titles: optional {sidecar entry name: its JSON "title"}, used to settle cases the
            file names alone cannot (two long names cut to the same sidecar name)
    """

    def __init__(self, names: Iterable[str], titles: Mapping[str, str] | None = None) -> None:
        self._names = set(names)
        self._dirs: dict[str, _Directory] = {}
        for entry in self._names:
            parent, basename = _split_entry(entry)
            if not basename:
                continue
            directory = self._dirs.setdefault(parent, _Directory())
            if basename.endswith(".json"):
                directory.add_sidecar(basename)
            elif is_media(basename):
                directory.media.add(basename)
        for entry, title in (titles or {}).items():
            parent, basename = _split_entry(entry)
            if isinstance(title, str) and parent in self._dirs:
                self._dirs[parent].titles[basename] = title

    def __contains__(self, name: object) -> bool:
        return name in self._names

    def __len__(self) -> int:
        return len(self._names)

    def match(self, media_entry: str, json_title_index: Mapping[str, str] | None = None) -> str | None:
        """Return the sidecar entry for a media entry, or None."""
        parent, basename = _split_entry(media_entry)
        directory = self._dirs.get(parent)
        if directory is None:
            directory = self._dirs[parent] = _Directory()
        if basename not in directory.media:
            directory.media.add(basename)
            directory.resolved = None
        if directory.resolved is None:
            directory.resolved = _resolve_directory(directory)
        found = directory.resolved.get(basename)
        if found is not None:
            return parent + found

        # Last resort: a caller-supplied {title: sidecar entry} mapping. Only a sidecar in
        # the same directory that no other file matched is accepted.
        if json_title_index:
            candidate = json_title_index.get(basename)
            if isinstance(candidate, str):
                cand_parent, cand_base = _split_entry(candidate)
                if cand_parent == parent and cand_base not in set(directory.resolved.values()):
                    return candidate
        return None


def _candidates(directory: _Directory, name: str, index: int | None) -> list[tuple[int, int, str]]:
    """Sidecars for (name, index), best first: full name tiers, then stem-only, then truncated."""
    found = list(directory.by_target.get((name, index), ()))
    stem = os.path.splitext(name)[0]
    if stem != name:
        for tier, tie, basename in directory.by_target.get((stem, index), ()):
            found.append((_STEM_OLD if tier == _PLAIN else _STEM_NEW, tie, basename))
    length = _utf16_len(name)
    for units in TRUNCATED_LENGTHS:
        if length > units:
            for tier, _, basename in directory.by_target.get((_utf16_cut(name, units), index), ()):
                if tier == _PLAIN:
                    found.append((_TRUNCATED, units, basename))
    return sorted(found)


def _strict_candidates(directory: _Directory, basename: str) -> list[tuple[int, int, str]]:
    """Candidates for a media name read literally, then with its last "(N)" as a duplicate index."""
    found = [(tier, 0, tie, side) for tier, tie, side in _candidates(directory, basename, None)]
    stripped, index = _strip_last_index(basename)
    if index is not None:
        found += [(tier, 1, tie, side) for tier, tie, side in _candidates(directory, stripped, index)]
    return [(tier, tie, side) for tier, _, tie, side in sorted(found)]


def _with_index(name: str, index: int | None) -> str:
    """Put a duplicate index back in front of the extension: ("a.jpg", 1) -> "a(1).jpg"."""
    if index is None:
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem}({index}){ext}"


def _derived_names(basename: str) -> list[str]:
    """Names whose sidecar this file shares: the original of an edited copy, the still of a live video."""
    names: list[str] = []
    stripped, index = _strip_last_index(unicodedata.normalize("NFC", basename))
    stem, ext = os.path.splitext(stripped)
    edited = _EDITED_STEM.match(stem)
    if edited and ext:
        names.append(_with_index(edited.group(1) + ext, index))
    if ext.lower() in LIVE_VIDEO_EXTENSIONS:
        names.extend(_with_index(stem + still, index) for still in STILL_EXTENSIONS)
    return names


def _title_matches(title: str | None, basename: str) -> bool:
    return title is not None and title in (basename, _strip_last_index(basename)[0])


def _resolve_directory(directory: _Directory) -> dict[str, str | None]:
    """Assign a sidecar (or None) to every media file of a directory."""
    resolved: dict[str, str | None] = {}
    claimed: set[str] = set()
    pending = sorted(directory.media)

    # 1. Strict matches on the file's own name.
    truncated_wants: dict[str, list[str]] = {}
    for media in pending:
        candidates = _strict_candidates(directory, media)
        exact = [side for tier, _, side in candidates if tier != _TRUNCATED]
        if exact:
            resolved[media] = exact[0]
            claimed.add(exact[0])
        else:
            for _, _, side in candidates:
                truncated_wants.setdefault(side, []).append(media)

    # 2. Truncated names: a cut name can be the prefix of several files, so it is only
    #    accepted when exactly one file wants it, or when the sidecar's title says which.
    for side in sorted(truncated_wants):
        wanting = [m for m in truncated_wants[side] if m not in resolved]
        if side in claimed or not wanting:
            continue
        if len(wanting) > 1:
            wanting = [m for m in wanting if _title_matches(directory.titles.get(side), m)]
        if len(wanting) == 1:
            resolved[wanting[0]] = side
            claimed.add(side)

    # 3. Edited copies and live-photo videos share the sidecar of the file they derive from.
    for media in pending:
        if media in resolved:
            continue
        for name in _derived_names(media):
            shared = [side for tier, _, side in _strict_candidates(directory, name) if tier <= _PLAIN]
            if shared:
                resolved[media] = shared[0]
                break

    # 4. Titles: a sidecar nobody matched whose JSON title is this file's name.
    #    A title equal to the full file name is decisive whatever "(N)" the sidecar
    #    carries: two long names cut to the same sidecar name are told apart that way.
    #    A title equal to the name without its duplicate index must agree on the index.
    by_title: dict[str, list[tuple[int, str]]] = {}
    for side, title in directory.titles.items():
        side_index = _strip_last_index(side[: -len(".json")])[1]
        by_title.setdefault(title, []).append((-1 if side_index is None else side_index, side))
    for media in pending:
        if media in resolved:
            continue
        stripped, index = _strip_last_index(media)
        free = [side for _, side in sorted(by_title.get(media, ())) if side not in claimed]
        if not free and index is not None:
            free = [
                side for side_index, side in sorted(by_title.get(stripped, ()))
                if side not in claimed and side_index == index
            ]
        if free:
            resolved[media] = free[0]
            claimed.add(free[0])

    # 5. Borrowing: a sidecar for the same name with a different duplicate index, but only
    #    one that no file in this directory matched (its own file is not in the zip).
    for media in pending:
        if media in resolved:
            continue
        stripped, index = _strip_last_index(media)
        options: list[tuple[int, int, int, int, str]] = []
        for (target, other), entries in directory.by_target.items():
            if other == index:
                continue
            for tier, tie, side in entries:
                if target == stripped:
                    rank = tier
                elif target == os.path.splitext(stripped)[0] and target != stripped:
                    rank = _STEM_OLD if tier == _PLAIN else _STEM_NEW
                else:
                    continue
                options.append((rank, 0 if other is None else 1, other or 0, tie, side))
        for *_, side in sorted(options):
            if side not in claimed:
                resolved[media] = side
                break

    for media in pending:
        resolved.setdefault(media, None)
    return resolved


_last_index: tuple[Any, int, SidecarIndex] | None = None


def find_json_match(
    nameset: Iterable[str] | SidecarIndex, media_entry: str, json_title_index: Mapping[str, str] | None = None
) -> str | None:
    """Find the JSON sidecar for a media file within a zip's namelist.

    Args:
        nameset: all entry names in the zip (any iterable), or a prebuilt SidecarIndex.
                 Passing the same set again reuses the index built for it.
        media_entry: full path of the media file in the zip
        json_title_index: optional dict mapping JSON 'title' field -> JSON entry path,
                          used as a last resort within the media file's directory

    Returns:
        The matching JSON entry path, or None.
    """
    global _last_index
    if isinstance(nameset, SidecarIndex):
        index = nameset
    elif isinstance(nameset, (set, frozenset, list, tuple)):
        if _last_index is not None and _last_index[0] is nameset and _last_index[1] == len(nameset):
            index = _last_index[2]
        else:
            index = SidecarIndex(nameset)
            _last_index = (nameset, len(nameset), index)
    else:
        index = SidecarIndex(nameset)
    return index.match(media_entry, json_title_index)


# ---------------------------------------------------------------------------
# METADATA PARSING
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_to_exif(value: Any) -> str | None:
    """Convert a sidecar epoch timestamp to "YYYY:MM:DD HH:MM:SS" in UTC, or None.

    Zero means "unknown" in Takeout sidecars. Negative values are real dates before 1970
    (scanned prints, for example), so they are kept. The date is computed by adding to the
    epoch rather than with ``datetime.fromtimestamp``, which rejects negative values on
    Windows and raises on out-of-range ones.
    """
    if isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds == 0:
        return None
    try:
        dt = _EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return None
    return f"{dt.year:04d}:{dt.month:02d}:{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def _finite_number(value: Any) -> float | None:
    """Return value if it is a real, finite number (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def parse_takeout_json(data: Any) -> dict[str, Any]:
    """Extract all usable metadata from a Google Takeout JSON sidecar.

    Returns dict with keys: datetime, latitude, longitude, altitude,
    description, people, title, favorited

    Sidecars are untrusted input: a field with an unexpected type or an impossible
    value is skipped, never raised on, so one odd sidecar cannot abort a run.
    """
    metadata: dict[str, Any] = {}
    if not isinstance(data, dict):
        return metadata

    # Timestamp: prefer photoTakenTime, fall back to creationTime
    for time_key in ("photoTakenTime", "creationTime"):
        block = data.get(time_key)
        if "datetime" not in metadata and isinstance(block, dict):
            text = _timestamp_to_exif(block.get("timestamp"))
            if text:
                metadata["datetime"] = text

    # GPS coordinates. Takeout writes 0.0/0.0 when there is no location.
    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key)
        if "latitude" in metadata or not isinstance(geo, dict):
            continue
        lat = _finite_number(geo.get("latitude"))
        lng = _finite_number(geo.get("longitude"))
        if lat is None or lng is None or (lat == 0 and lng == 0):
            continue
        if abs(lat) > 90 or abs(lng) > 180:
            continue
        metadata["latitude"] = lat
        metadata["longitude"] = lng
        metadata["altitude"] = _finite_number(geo.get("altitude")) or 0

    # Description
    desc = data.get("description")
    if isinstance(desc, str) and desc.strip():
        metadata["description"] = desc.strip()

    # People tags
    people = data.get("people")
    if isinstance(people, list):
        names = [
            p["name"].strip() for p in people
            if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip()
        ]
        if names:
            metadata["people"] = names

    # Favorite status
    if data.get("favorited") is True:
        metadata["favorited"] = True

    # Title (for reference)
    title = data.get("title")
    if isinstance(title, str) and title:
        metadata["title"] = title

    return metadata


# ---------------------------------------------------------------------------
# EXIFTOOL COMMAND BUILDING
# ---------------------------------------------------------------------------

def _format_number(value: float) -> str:
    """Format a coordinate as plain decimal text (never exponent notation)."""
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def is_video(name: str | os.PathLike[str]) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def build_exiftool_args(
    metadata: dict[str, Any], filepath: str | os.PathLike[str], *, write_dates: bool = True
) -> list[str]:
    """Build exiftool arguments to embed all metadata into a file.

    ``metadata["datetime"]`` is a UTC wall-clock time. With ``write_dates=False`` the
    date tags are left out (used when the file already holds a consistent capture time).
    """
    args: list[str] = []

    if "datetime" in metadata and write_dates:
        dt = metadata["datetime"]
        if is_video(filepath):
            # QuickTime date fields are UTC by specification. DateTimeOriginal is stored as
            # text with a zone; without an explicit one ExifTool appends the zone of the
            # machine it runs on, which would make the result depend on where it is run.
            args.extend([
                f"-DateTimeOriginal={dt}+00:00",
                f"-CreateDate={dt}",
                f"-ModifyDate={dt}",
                f"-TrackCreateDate={dt}",
                f"-TrackModifyDate={dt}",
                f"-MediaCreateDate={dt}",
                f"-MediaModifyDate={dt}",
            ])
        else:
            # EXIF dates carry no zone, so the UTC offset is written next to them.
            # Without it a stale OffsetTime tag would shift the instant.
            args.extend([
                f"-DateTimeOriginal={dt}",
                f"-CreateDate={dt}",
                f"-ModifyDate={dt}",
                "-OffsetTimeOriginal=+00:00",
                "-OffsetTimeDigitized=+00:00",
                "-OffsetTime=+00:00",
            ])

    if "latitude" in metadata:
        lat, lng = metadata["latitude"], metadata["longitude"]
        # The value carries the sign as well as the Ref tag. EXIF stores an unsigned value
        # plus a reference letter, but containers that only take XMP (MP4, MOV, GIF) keep
        # the hemisphere inside the value itself, so an unsigned value would always be
        # read back as north/east there.
        args.extend([
            f"-GPSLatitude={_format_number(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={_format_number(lng)}",
            f"-GPSLongitudeRef={'E' if lng >= 0 else 'W'}",
        ])
        alt = metadata.get("altitude", 0)
        if alt != 0:
            # "#" writes the raw number: without it ExifTool parses "1" as text and
            # stores 0 (above sea level).
            args.extend([
                f"-GPSAltitude={_format_number(abs(alt))}",
                f"-GPSAltitudeRef#={0 if alt >= 0 else 1}",
            ])

    if "description" in metadata:
        args.append(f"-ImageDescription={metadata['description']}")

    if "people" in metadata:
        for name in metadata["people"]:
            args.append(f"-XMP:PersonInImage={name}")

    return args


class ExifResult(NamedTuple):
    """Outcome of one file in an ExifTool batch."""

    ok: bool
    message: str = ""


# Options applied to every file in a batch.
#   -m                        ignore minor warnings
#   -overwrite_original       do not leave "_original" backup copies
#   -E                        decode HTML entities in written values (see _argfile_line)
#   -charset filename=utf8    file names in the argfile are UTF-8 (needed on Windows)
#   -api LargeFileSupport=1   allow videos over 4 GiB on older ExifTool releases
EXIFTOOL_COMMON_ARGS = ["-m", "-overwrite_original", "-E", "-charset", "filename=utf8", "-api", "LargeFileSupport=1"]
EXIFTOOL_TIMEOUT = 600  # seconds per batch

_MARKER = re.compile(r"\{gtmf:(\d+)(?::(\d+|\$\{status\}))?\}")


def _argfile_line(arg: str) -> str:
    r"""Encode one ``-TAG=VALUE`` argument as a single ExifTool argfile line.

    An argfile holds one argument per line, so a value containing a line break (a
    multi-line caption, for example) would otherwise be split into several arguments,
    and its later lines would be read as file names or options. ExifTool is run with
    ``-E``, which makes it decode HTML character entities in the values it writes, so
    every character that could break out of the line or be misread is sent as an entity.

    (ExifTool's ``#[CSTR]`` line prefix looks like the obvious tool for this, but it
    turns every ``$`` and ``@`` into ``\$`` and ``\@``, which corrupts ordinary captions.)
    """
    name, separator, value = arg.partition("=")
    encoded = "".join(
        f"&#x{ord(char):x};" if char in "&<>\"'" or ord(char) < 0x20 or char == "\x7f" else char
        for char in value.replace("\x00", "")
    )
    # ExifTool drops one space after "=" and ignores trailing white space
    if encoded.startswith(" "):
        encoded = "&#x20;" + encoded[1:]
    if encoded.endswith(" "):
        encoded = encoded[:-1] + "&#x20;"
    return name + separator + encoded


def _argfile_path_line(path: str) -> str | None:
    """Encode a file path as an argfile line, or None if it cannot be passed safely.

    Paths are absolute, so they never start with "-", "#" or white space. A path with a
    line break needs the ``#[CSTR]`` form, which cannot carry "$" or "@" (see above).
    """
    if "\n" not in path and "\r" not in path:
        return path
    if "$" in path or "@" in path:
        return None
    return "#[CSTR]" + path.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _split_on_markers(text: str) -> dict[int, tuple[str, str | None]]:
    """Map block number -> (text printed for that block, status field of its marker)."""
    blocks: dict[int, tuple[str, str | None]] = {}
    position = 0
    for match in _MARKER.finditer(text):
        blocks[int(match.group(1))] = (text[position:match.start()], match.group(2))
        position = match.end()
    return blocks


def _parse_exiftool_output(stdout: str, stderr: str, count: int) -> list[ExifResult | None]:
    """Turn the marker-delimited ExifTool output into one result per file.

    A file whose marker is missing gets ``None``: ExifTool never reported on it
    (it crashed, was killed or timed out first), so nothing is known about that file.
    """
    out_blocks = _split_on_markers(stdout)
    err_blocks = _split_on_markers(stderr)
    results: list[ExifResult | None] = []
    for i in range(count):
        if i not in out_blocks:
            results.append(None)
            continue
        text, status = out_blocks[i]
        if status is not None and status.isdigit():
            ok = status == "0"
        else:
            # ExifTool releases without ${status} support print the marker literally;
            # fall back to the summary line of the block.
            ok = "1 image files updated" in text and "weren't updated" not in text
        message = ""
        if not ok:
            lines = [line.strip() for line in err_blocks.get(i, ("", None))[0].splitlines() if line.strip()]
            errors = [line for line in lines if line.startswith("Error")]
            message = (errors or lines or ["ExifTool reported an error"])[0][:300]
        results.append(ExifResult(ok, message))
    return results


def run_exiftool_batch_detailed(
    file_args_list: Sequence[tuple[str | os.PathLike[str], Sequence[str]]],
) -> list[ExifResult | None]:
    """Run ExifTool once for a batch of files and report the outcome of each file.

    file_args_list: sequence of (filepath, [arg1, arg2, ...]).
    Returns one entry per input file, in order: an ``ExifResult``, or ``None`` when
    ExifTool gave no result for that file (not installed, timed out, crashed).

    The arguments are sent on standard input as an argfile, one ``-execute`` block per
    file. Every block ends with a marker carrying ExifTool's exit status for that block,
    which is how results are attributed to individual files.
    """
    count = len(file_args_list)
    if count == 0:
        return []

    lines: list[str] = []
    unsupported: set[int] = set()
    for i, (filepath, args) in enumerate(file_args_list):
        # Absolute path: a relative name starting with "-" would be read as an option.
        path_line = _argfile_path_line(os.path.abspath(os.fspath(filepath)))
        if path_line is None:
            unsupported.add(i)
            continue
        if lines:
            lines.append("-execute")
        lines.extend(_argfile_line(a) for a in args)
        lines.append(path_line)
        lines.extend(["-echo3", f"{{gtmf:{i}:${{status}}}}", "-echo4", f"{{gtmf:{i}}}"])
    results: list[ExifResult | None] = [None] * count
    if lines:
        payload = ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")
        try:
            proc = subprocess.run(
                ["exiftool", "-@", "-", "-common_args", *EXIFTOOL_COMMON_ARGS],
                input=payload, capture_output=True, timeout=EXIFTOOL_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return results
        results = _parse_exiftool_output(
            proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"), count
        )
    for i in unsupported:
        results[i] = ExifResult(False, "Error: file name contains a line break together with $ or @")
    return results


def run_exiftool_batch(
    file_args_list: Sequence[tuple[str | os.PathLike[str], Sequence[str]]],
) -> tuple[int, int]:
    """Run ExifTool on a batch of files. Returns (updated_count, not_updated_count).

    Thin wrapper around :func:`run_exiftool_batch_detailed` for callers that only need totals.
    """
    results = run_exiftool_batch_detailed(file_args_list)
    updated = sum(1 for r in results if r is not None and r.ok)
    return updated, len(results) - updated


# ---------------------------------------------------------------------------
# DATE POLICY AND BATCH APPLICATION
#
# A sidecar stores the capture instant in UTC. A photo that still has its camera
# metadata stores the *local* wall-clock time, usually with the zone offset in a
# separate tag. Overwriting that with UTC loses the local time and, when the old
# offset tag stays behind, moves the instant. So a photo's embedded capture time
# is kept whenever it shows the same instant as the sidecar up to a time-zone
# offset, and replaced only when it is missing or really different.
# ---------------------------------------------------------------------------

_EXIF_DATETIME = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_ZONE_STEP = 15 * 60          # every real time zone is a multiple of 15 minutes
_ZONE_TOLERANCE = 2           # seconds of rounding allowed
_MIN_ZONE, _MAX_ZONE = -12 * 3600, 14 * 3600


def _parse_exif_datetime(text: str | None) -> datetime | None:
    match = _EXIF_DATETIME.match(text) if isinstance(text, str) else None
    if not match:
        return None
    try:
        return datetime(*(int(g) for g in match.groups()))  # type: ignore[arg-type]
    except ValueError:
        return None


def camera_time_matches(existing: str | None, sidecar_utc: str) -> bool:
    """True when an embedded capture time is the sidecar instant shifted by a time zone.

    ``existing`` is the file's DateTimeOriginal (local wall-clock time) and ``sidecar_utc``
    the sidecar time in UTC, both as "YYYY:MM:DD HH:MM:SS". They match when they differ
    by a whole number of quarter hours between -12:00 and +14:00.
    """
    local, utc = _parse_exif_datetime(existing), _parse_exif_datetime(sidecar_utc)
    if local is None or utc is None:
        return False
    delta = (local - utc).total_seconds()
    if not _MIN_ZONE - _ZONE_TOLERANCE <= delta <= _MAX_ZONE + _ZONE_TOLERANCE:
        return False
    return abs(delta - round(delta / _ZONE_STEP) * _ZONE_STEP) <= _ZONE_TOLERANCE


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def read_existing_dates(paths: Sequence[str | os.PathLike[str]]) -> dict[str, str]:
    """Read the embedded DateTimeOriginal of each file with one ExifTool call.

    Returns {normalised absolute path: date text} for the files that have one. Files that
    cannot be read are simply absent, as is everything if ExifTool is unavailable.
    """
    if not paths:
        return {}
    lines = [line for line in (_argfile_path_line(os.path.abspath(os.fspath(p))) for p in paths) if line]
    if not lines:
        return {}
    payload = ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")
    try:
        proc = subprocess.run(
            ["exiftool", "-json", "-charset", "filename=utf8", "-api", "LargeFileSupport=1",
             "-DateTimeOriginal", "-@", "-"],
            input=payload, capture_output=True, timeout=EXIFTOOL_TIMEOUT,
        )
        records = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {}
    found: dict[str, str] = {}
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and isinstance(record.get("DateTimeOriginal"), str):
            found[_path_key(str(record.get("SourceFile", "")))] = record["DateTimeOriginal"]
    return found


class ApplyResult(NamedTuple):
    """Outcome of applying one file's metadata.

    status is "written" (ExifTool updated the file), "kept" (nothing needed writing),
    "failed" (ExifTool reported an error for this file) or "unknown" (no result).
    """

    status: str
    message: str = ""
    date_kept: bool = False


def apply_metadata_batch(
    items: Sequence[tuple[str | os.PathLike[str], dict[str, Any]]], *, overwrite_dates: bool = False
) -> list[ApplyResult]:
    """Write parsed sidecar metadata into a batch of files. One result per item, in order.

    Unless ``overwrite_dates`` is set, a photo's embedded capture time is read first and
    kept when it is consistent with the sidecar (see :func:`camera_time_matches`).
    Videos always get the sidecar time, because their date fields are UTC by specification.
    """
    existing: dict[str, str] = {}
    if not overwrite_dates:
        existing = read_existing_dates(
            [path for path, metadata in items if "datetime" in metadata and not is_video(path)]
        )

    plans: list[tuple[list[str], bool]] = []
    for path, metadata in items:
        keep = (
            not overwrite_dates
            and "datetime" in metadata
            and not is_video(path)
            and camera_time_matches(existing.get(_path_key(path)), metadata["datetime"])
        )
        plans.append((build_exiftool_args(metadata, path, write_dates=not keep), keep))

    to_run = [(path, args) for (path, _), (args, _) in zip(items, plans, strict=True) if args]
    results = iter(run_exiftool_batch_detailed(to_run))
    outcome: list[ApplyResult] = []
    for args, keep in plans:
        if not args:
            outcome.append(ApplyResult("kept", "", keep))
            continue
        result = next(results)
        if result is None:
            outcome.append(ApplyResult("unknown", "ExifTool gave no result for this file", keep))
        elif result.ok:
            outcome.append(ApplyResult("written", "", keep))
        else:
            outcome.append(ApplyResult("failed", result.message, keep))
    return outcome


# ---------------------------------------------------------------------------
# WHICH OUTPUT FILE BELONGS TO A ZIP ENTRY
#
# Output folders are flat, so two zip entries with the same file name (the same camera
# counter in two years, or one photo listed in two albums) compete for one name. An
# extractor that keeps both writes the second as "name_1.jpg". Matching on the name alone
# would write one photo's metadata onto the other, so a file is only used when something
# identifies it: a target recorded by an earlier run, the exact size of the zip entry
# (an untouched file), or a capture time already in the file that agrees with the sidecar.
# When nothing identifies it, the entry is reported as ambiguous and left alone.
# ---------------------------------------------------------------------------

_RENAMED_COPY = re.compile(r"^(.*)_(\d+)$")
_DATE_PREREAD = 1000  # files whose dates are read ahead in one ExifTool call


class TargetResolver:
    """Finds the file in the output folder that a zip entry's metadata should be written to."""

    def __init__(self, output_dir: Path, store: ProgressStore | None = None) -> None:
        self.files: dict[str, Path] = {
            f.name: f for f in sorted(output_dir.iterdir()) if f.is_file() and is_media(f.name)
        }
        # "IMG_1.jpg" -> [IMG_1_1.jpg, IMG_1_2.jpg, ...]: copies renamed on a name clash
        self.renamed: dict[str, list[Path]] = {}
        numbered: dict[str, list[tuple[int, Path]]] = {}
        for name, path in self.files.items():
            stem, suffix = os.path.splitext(name)
            match = _RENAMED_COPY.match(stem)
            if match:
                numbered.setdefault(match.group(1) + suffix, []).append((int(match.group(2)), path))
        for name, copies in numbered.items():
            self.renamed[name] = [path for _, path in sorted(copies)]
        self.claims: dict[str, str] = {}                 # file name -> entry key that owns it
        self.applied: dict[str, dict[str, Any]] = {}     # file name -> metadata written this run
        for entry_key, name in (store.targets if store else {}).items():
            self.claims.setdefault(name, entry_key)
        self._dates: dict[str, str | None] = {}
        self._contested: list[Path] = []
        self._same_name: Counter[str] = Counter()
        self._zip_names: set[str] = set()

    def __len__(self) -> int:
        return len(self.files)

    def start_zip(self, media_entries: Iterable[str]) -> None:
        """Tell the resolver which media entries the zip about to be processed holds."""
        basenames = [_split_entry(entry)[1] for entry in media_entries]
        self._same_name = Counter(basenames)
        self._zip_names = set(basenames)
        self._contested = [
            path for name in sorted(self._zip_names)
            if self._same_name[name] > 1 or name in self.renamed
            for path in self.candidates(name)
        ]

    def candidates(self, filename: str) -> list[Path]:
        """Output files that could hold this entry: the name itself, then its renamed copies."""
        found = [self.files[filename]] if filename in self.files else []
        return found + self.renamed.get(filename, [])

    def _existing_date(self, path: Path) -> str | None:
        key = _path_key(path)
        if key not in self._dates:
            # Read this file together with other contested files of the zip: one ExifTool
            # call for many files instead of one call each.
            batch = [path] + [p for p in self._contested if _path_key(p) not in self._dates][:_DATE_PREREAD]
            found = read_existing_dates(batch)
            for p in batch:
                self._dates[_path_key(p)] = found.get(_path_key(p))
        return self._dates[key]

    def resolve(self, entry_key: str, filename: str, size: int, metadata: dict[str, Any]) -> tuple[Path | None, str]:
        """Return (file, "ok"), (None, "no_match") or (None, "ambiguous")."""
        candidates = self.candidates(filename)
        if not candidates:
            return None, "no_match"
        for path in candidates:
            if self.claims.get(path.name) == entry_key:
                return path, "ok"
        free = [path for path in candidates if path.name not in self.claims]
        exact = self.files.get(filename)
        single_entry = self._same_name[filename] <= 1

        # The ordinary case needs no evidence: one entry and one file with this name.
        if single_entry and len(candidates) == 1 and free:
            if candidates[0] is exact or candidates[0].name not in self._zip_names:
                return candidates[0], "ok"

        # An untouched file has exactly the size recorded in the zip.
        for path in free:
            try:
                if path.stat().st_size == size:
                    return path, "ok"
            except OSError:
                continue

        # The same photo listed twice (two albums): its file already got this very metadata.
        for path in candidates:
            if path.name in self.applied and _same_photo(self.applied[path.name], metadata):
                return path, "ok"

        # A capture time already in the file that agrees with this sidecar.
        sidecar_time = metadata.get("datetime")
        if sidecar_time:
            unwritten = [path for path in candidates if path.name not in self.applied]
            agreeing = [path for path in unwritten if camera_time_matches(self._existing_date(path), sidecar_time)]
            agreeing.sort(key=lambda path: path.name in self.claims)  # unclaimed files first
            if agreeing:
                return agreeing[0], "ok"

        if single_entry:
            if exact is not None and exact in free:
                return exact, "ok"
            if len(free) == 1 and free[0].name not in self._zip_names:
                return free[0], "ok"
        return None, "ambiguous"

    def claim(self, entry_key: str, path: Path, metadata: dict[str, Any]) -> None:
        self.claims.setdefault(path.name, entry_key)
        self.applied[path.name] = metadata


def _same_photo(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Two sidecars describe the same photo when their capture times are equal."""
    if "datetime" in first and "datetime" in second:
        return bool(first["datetime"] == second["datetime"])
    return first == second


# ---------------------------------------------------------------------------
# ADB HELPERS
# ---------------------------------------------------------------------------

class AdbError(OSError):
    """An adb command could not be run, timed out, or reported failure."""


def sh_quote(text: str) -> str:
    """Quote one argument for the shell on the device.

    ``adb shell`` hands its command line to a POSIX shell on the phone. Paths and file
    names (zip names come from a directory listing, entry names from inside the zip) are
    data, so every one of them is quoted: otherwise a space splits the argument and
    characters such as ``$( )``, backticks or quotes are interpreted by that shell.
    """
    return shlex.quote(text)


def unzip_literal(entry: str) -> str:
    """Escape an entry name so ``unzip`` matches it literally instead of as a wildcard pattern."""
    return re.sub(r"([\\\[\]*?])", r"\\\1", entry)


def adb_shell(command: str, *, timeout: int, text: bool = False) -> subprocess.CompletedProcess[Any]:
    """Run one command line in the device shell. Raises AdbError if adb is missing or times out."""
    kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    try:
        return subprocess.run(["adb", "shell", command], **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb did not answer within {timeout} s") from exc
    except FileNotFoundError as exc:
        raise AdbError("adb was not found on PATH") from exc


def parse_adb_devices(output: str) -> list[str]:
    """Serial numbers of the devices ``adb devices`` reports as ready.

    Only the state "device" counts. "unauthorized", "offline" and "no permissions"
    lines are not usable devices, whatever other words they contain.
    """
    serials = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and not line.startswith(("List of", "*")):
            serials.append(parts[0])
    return serials


def check_adb():
    """Verify ADB connection to an Android device."""
    try:
        r = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10
        )
    except FileNotFoundError:
        print("ERROR: ADB not found! Install Android SDK Platform Tools.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: adb did not answer. Is the adb server running?")
        sys.exit(1)
    devices = parse_adb_devices(r.stdout)
    if not devices:
        print("ERROR: No Android device found.")
        print("  Connect via USB and enable USB Debugging.")
        sys.exit(1)
    print(f"  ADB device: {devices[0]}")
    return True


def adb_list_zips(remote_dir):
    """List zip files on the Android device."""
    r = adb_shell(f"ls -1 {sh_quote(remote_dir)}", timeout=30, text=True)
    files = [f.strip() for f in r.stdout.strip().split("\n") if f.strip()]
    zips = sorted([f for f in files if f.lower().endswith(".zip")])
    if not zips and r.returncode != 0:
        raise AdbError(f"could not list {remote_dir} on the device")
    return zips


def adb_get_zip_listing(remote_zip):
    """Get the list of all entries in a remote zip via ADB.

    Returns list of (size, name) tuples.
    """
    r = adb_shell(f"unzip -l {sh_quote(remote_zip)}", timeout=120, text=True)
    entries = []
    for line in r.stdout.split("\n"):
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and "/" in parts[3]:
            try:
                size = int(parts[0])
            except ValueError:
                continue
            entries.append((size, parts[3].strip()))
    return entries


def adb_stream_all_jsons(remote_zip):
    """Stream ALL JSON file contents from a remote zip via ADB.

    Returns the raw bytes of all concatenated JSON files.
    Uses 'unzip -p *.json' which is very fast (~0.3s for thousands of JSONs).
    """
    return adb_shell(f"unzip -p {sh_quote(remote_zip)} '*.json' 2>/dev/null", timeout=300).stdout


def adb_stream_single_json(remote_zip, json_entry):
    """Stream a single JSON file from a remote zip.

    Fallback for when batch parsing has issues.
    """
    command = f"unzip -p {sh_quote(remote_zip)} {sh_quote(unzip_literal(json_entry))} 2>/dev/null"
    return adb_shell(command, timeout=30).stdout


def _decode_json_values(raw_bytes: bytes) -> list[Any] | None:
    """Decode consecutive top-level JSON values. Returns None if anything fails to parse."""
    text = raw_bytes.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return values
        try:
            value, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            return None
        values.append(value)


def _loads_object(raw: bytes) -> dict[str, Any] | None:
    """Parse one JSON document; only an object counts as a sidecar."""
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _fold(text: str) -> str:
    """Approximate what Takeout does to a title when it becomes a file name."""
    text = unicodedata.normalize("NFC", text)
    return "".join(c if c.isalnum() or c in "._-() " else "_" for c in text)


def sidecar_name_matches_title(json_name: str, title: str) -> bool:
    """True if a sidecar's file name is plausible for the "title" stored inside it.

    The title is the original media file name. The sidecar name is derived from it, with
    some characters replaced, possibly cut short, possibly with a "(N)" added. This is a
    sanity check on a pairing made by other means, not a way to find the sidecar.
    """
    base = _split_entry(json_name)[1]
    if base.endswith(".json"):
        base = base[: -len(".json")]
    folded_title = _fold(title)
    expected = f"{folded_title}.{SUPPLEMENTAL}"
    title_stem = os.path.splitext(folded_title)[0]
    for candidate in (base, _strip_last_index(base)[0]):
        folded = _fold(candidate)
        if not expected.startswith(folded):
            continue
        cut_short = _utf16_len(candidate) in (45, *TRUNCATED_LENGTHS)
        if len(folded) >= len(folded_title) or folded == title_stem or cut_short:
            return True
    return False


def _looks_like_wrong_sidecar(name: str, value: dict[str, Any]) -> bool:
    title = value.get("title")
    return "photoTakenTime" in value and isinstance(title, str) and not sidecar_name_matches_title(name, title)


def pair_json_stream(
    raw_bytes: bytes, sized_names: Sequence[tuple[int, str]]
) -> tuple[dict[str, dict[str, Any]], str]:
    """Pair a stream of concatenated sidecars with their entry names.

    ``sized_names`` is the (uncompressed size, entry name) of every JSON entry, in listing
    order; ``raw_bytes`` is what ``unzip -p zip "*.json"`` printed. Returns
    ``({entry name: parsed object}, method)``:

    "sizes"  The listed sizes add up to the stream length, so the stream is cut at those
             offsets. An empty or non-object sidecar simply has no entry in the result.
    "order"  The sizes do not add up (an old adb translating line endings, for example).
             The stream is paired by position only if it holds exactly one JSON value per
             name and every sidecar's title agrees with its name.
    "none"   Neither holds. Nothing is paired; the caller reads sidecars one by one.

    Names missing from the result are the caller's to read individually.
    """
    paired: dict[str, dict[str, Any]] = {}
    if sum(size for size, _ in sized_names) == len(raw_bytes):
        position = 0
        for size, name in sized_names:
            value = _loads_object(raw_bytes[position:position + size])
            position += size
            if value is not None and not _looks_like_wrong_sidecar(name, value):
                paired[name] = value
        return paired, "sizes"

    values = _decode_json_values(raw_bytes)
    if values is None or len(values) != len(sized_names):
        return {}, "none"
    for (_, name), value in zip(sized_names, values, strict=True):
        if not isinstance(value, dict):
            continue
        if _looks_like_wrong_sidecar(name, value):
            return {}, "none"
        paired[name] = value
    return paired, "order"


def parse_concatenated_jsons(raw_bytes: bytes, json_names_ordered: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Parse a stream of concatenated JSON objects and pair them with names by position.

    Kept for callers that have names but no sizes; :func:`pair_json_stream` is safer and is
    what the fixer uses. Objects are paired with names only when there is exactly one
    object per name. With any other count the result is empty, because a single missing
    or extra object shifts every later pair onto the wrong name.

    Returns dict of {json_entry_path: parsed_dict}.
    """
    text = raw_bytes.decode("utf-8", errors="replace")

    # Decode consecutive top-level JSON objects. raw_decode understands string
    # literals, so braces inside descriptions/titles don't break the split.
    decoder = json.JSONDecoder()
    parsed_list: list[dict[str, Any] | None] = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            # Skip the unparseable object and resync at the next brace
            parsed_list.append(None)
            pos = start + 1
            continue
        parsed_list.append(obj if isinstance(obj, dict) else None)
        pos = end

    if len(parsed_list) != len(json_names_ordered):
        return {}
    return {name: obj for name, obj in zip(json_names_ordered, parsed_list, strict=True) if obj is not None}


# ---------------------------------------------------------------------------
# MAIN PROCESSING
# ---------------------------------------------------------------------------

def local_list_zips(local_dir):
    """List zip files in a local directory."""
    p = Path(local_dir)
    return sorted([f.name for f in p.glob("*.zip")])


def local_get_zip_listing(zip_path):
    """Get the list of all entries in a local zip.

    Returns list of (size, name) tuples.
    """
    entries = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if not info.is_dir():
                entries.append((info.file_size, info.filename))
    return entries


def local_stream_all_jsons(zip_path):
    """Read ALL JSON file contents from a local zip.

    Returns the raw bytes of all concatenated JSON files.
    """
    chunks = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.filename.endswith(".json") and not info.is_dir():
                chunks.append(zf.read(info.filename))
    return b"".join(chunks)


def local_read_jsons(zip_path: str | os.PathLike[str], names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Read sidecars from a local zip by entry name. Returns {entry name: parsed object}.

    Reading by name cannot pair a sidecar with the wrong name. Entries that are missing,
    unreadable or not a JSON object are left out.
    """
    found: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in names:
            try:
                value = _loads_object(zf.read(name))
            except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
                continue
            if value is not None:
                found[name] = value
    return found


def _local_read_sidecar(zip_path: str, json_entry: str) -> bytes:
    """Read one sidecar from a local zip; empty bytes if it cannot be read."""
    try:
        return local_stream_single_json(zip_path, json_entry)
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
        return b""


def local_stream_single_json(zip_path, json_entry):
    """Read a single JSON file from a local zip."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return zf.read(json_entry)


class Options(NamedTuple):
    dry_run: bool = False
    overwrite_dates: bool = False
    retry_failed: bool = False


def new_zip_stats() -> dict[str, Any]:
    return {
        "fixed": 0, "unchanged": 0, "no_json": 0, "no_match": 0, "no_meta": 0, "err": 0, "skip": 0,
        "ambiguous": 0, "failed_before": 0, "no_result": 0, "failures": [],
    }


def _apply_batch(
    batch: list[tuple[Path, dict[str, Any], str]],
    zip_stats: dict[str, Any], stats: dict[str, int], store: ProgressStore, options: Options,
) -> None:
    """Write one batch of (target, metadata, entry key) and record each outcome."""
    results = apply_metadata_batch(
        [(target, metadata) for target, metadata, _ in batch], overwrite_dates=options.overwrite_dates
    )
    for (target, _, entry_key), result in zip(batch, results, strict=True):
        if result.status in ("written", "kept"):
            zip_stats["fixed" if result.status == "written" else "unchanged"] += 1
            store.record(entry_key, "done", target=target.name)
        elif result.status == "failed":
            zip_stats["err"] += 1
            zip_stats["failures"].append(f"{target.name}: {result.message}")
            store.record(entry_key, "failed", message=result.message, target=target.name)
        else:
            zip_stats["no_result"] += 1  # not recorded: the next run tries again
        if result.date_kept:
            stats["dates_kept"] += 1


def fix_zip_entries(
    zip_name: str,
    entries: Sequence[tuple[int, str]],
    json_data_map: Mapping[str, dict[str, Any]],
    read_sidecar: Callable[[str], bytes] | None,
    resolver: TargetResolver,
    store: ProgressStore,
    options: Options,
    stats: dict[str, int],
) -> dict[str, Any]:
    """Match, parse and write the metadata of every media entry of one zip.

    entries:       (uncompressed size, entry name) for every entry in the zip
    json_data_map: sidecars already read, {entry name: parsed object}
    read_sidecar:  reads one sidecar that is not in json_data_map (or None if not possible)

    Returns the counters for this zip. ``no_result`` counts entries whose outcome is
    unknown; while it is not zero the zip must not be marked completed.
    """
    media_entries = [(size, name) for size, name in entries if is_media(name) and not name.endswith("/")]
    titles = {
        name: data["title"] for name, data in json_data_map.items()
        if isinstance(data.get("title"), str) and data["title"]
    }
    sidecar_index = SidecarIndex([name for _, name in entries], titles)
    resolver.start_zip(name for _, name in media_entries)

    print(f"  Processing {len(media_entries)} media files...")
    started = time.time()
    zip_stats = new_zip_stats()
    batch: list[tuple[Path, dict[str, Any], str]] = []

    for i, (size, media_entry) in enumerate(media_entries, 1):
        entry_key = f"{zip_name}::{media_entry}"
        filename = _split_entry(media_entry)[1]

        if entry_key in store.processed:
            zip_stats["skip"] += 1
        elif entry_key in store.failed and not options.retry_failed:
            zip_stats["failed_before"] += 1
        elif not resolver.candidates(filename):
            zip_stats["no_match"] += 1
        else:
            metadata: dict[str, Any] | None = None
            json_entry = sidecar_index.match(media_entry)
            if not json_entry:
                zip_stats["no_json"] += 1
            else:
                json_data = json_data_map.get(json_entry)
                raw = b""
                if json_data is None and read_sidecar is not None:
                    try:
                        raw = read_sidecar(json_entry)  # not in the batch read: read it on its own
                    except AdbError:
                        zip_stats["no_result"] += 1  # the device did not answer: try again next run
                        continue
                    json_data = _loads_object(raw)
                if json_data is None and not raw:
                    zip_stats["no_json"] += 1
                else:
                    metadata = parse_takeout_json(json_data)
                    if not metadata or not build_exiftool_args(metadata, filename):
                        zip_stats["no_meta"] += 1
                        metadata = None

            if metadata is None:
                if not options.dry_run:
                    store.record(entry_key, "done")
            else:
                target, why = resolver.resolve(entry_key, filename, size, metadata)
                if target is None:
                    zip_stats["ambiguous" if why == "ambiguous" else "no_match"] += 1
                else:
                    for key in ("datetime", "latitude", "description", "people"):
                        if key in metadata:
                            stats["has_gps" if key == "latitude" else f"has_{key}"] += 1
                    resolver.claim(entry_key, target, metadata)
                    if options.dry_run:
                        zip_stats["fixed"] += 1
                    else:
                        batch.append((target, metadata, entry_key))
                        if len(batch) >= EXIFTOOL_BATCH_SIZE:
                            _apply_batch(batch, zip_stats, stats, store, options)
                            batch = []

        if i % 200 == 0 or i == len(media_entries):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(media_entries) - i) / rate if rate > 0 else 0
            print(
                f"    [{i}/{len(media_entries)}] "
                f"fixed={zip_stats['fixed']} no_json={zip_stats['no_json']} "
                f"no_match={zip_stats['no_match']} skip={zip_stats['skip']} "
                f"| {rate:.0f}/s | ETA: {format_time(eta)}",
                flush=True,
            )

    if batch:
        _apply_batch(batch, zip_stats, stats, store, options)
    zip_stats["elapsed"] = time.time() - started
    return zip_stats


def format_zip_summary(zip_stats: dict[str, Any]) -> str:
    """The one-line result of a zip. New counters are appended, and only when not zero."""
    line = (
        f"fixed={zip_stats['fixed']} no_json={zip_stats['no_json']} "
        f"no_match={zip_stats['no_match']} skip={zip_stats['skip']} "
        f"err={zip_stats['err']} unchanged={zip_stats['unchanged']}"
    )
    for key in ("ambiguous", "failed_before", "no_result"):
        if zip_stats[key]:
            line += f" {key}={zip_stats[key]}"
    return line


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix metadata on Google Takeout photos using JSON sidecars.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # ADB mode (zips on Android phone):
    python google_takeout_metadata_fixer.py --output ./Output --phone-dir /sdcard/Download

  # Local mode (zips on your computer):
    python google_takeout_metadata_fixer.py --output ./Output --local-zips ./takeout-zips/

  # Dry run:
    python google_takeout_metadata_fixer.py --output ./Output --local-zips ./zips/ --dry-run
"""
    )
    parser.add_argument(
        "--output", required=True,
        help="Directory containing extracted photos/videos to fix"
    )
    parser.add_argument(
        "--phone-dir", default="/sdcard/Download",
        help="Directory on phone containing takeout zips (ADB mode)"
    )
    parser.add_argument(
        "--local-zips",
        help="Local directory containing takeout zip files (skips ADB)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without modifying any files"
    )
    parser.add_argument(
        "--overwrite-dates", action="store_true",
        help="Always write the sidecar date. By default a photo's embedded capture time is "
             "kept when it shows the same instant as the sidecar in another time zone"
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Try again the files ExifTool reported an error for in an earlier run"
    )
    args = parser.parse_args()
    options = Options(dry_run=args.dry_run, overwrite_dates=args.overwrite_dates, retry_failed=args.retry_failed)

    use_local = args.local_zips is not None
    output_dir = Path(args.output)
    if not output_dir.exists():
        print(f"ERROR: Output directory not found: {args.output}")
        sys.exit(1)

    mode_label = "Local zip mode" if use_local else "ADB mode (streams JSONs from phone)"
    print("=" * 65)
    print("  Google Takeout Metadata Fixer")
    print(f"  {mode_label}")
    print("=" * 65)

    # Check prerequisites
    print("\nChecking prerequisites...")
    if not use_local:
        check_adb()
    try:
        r = subprocess.run(
            ["exiftool", "-ver"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10
        )
        print(f"  ExifTool: v{r.stdout.strip()}")
    except FileNotFoundError:
        print("ERROR: ExifTool not found on PATH!")
        sys.exit(1)

    # Load progress, then index the output files by name
    store = ProgressStore(dry_run=options.dry_run)
    print(f"\nIndexing output files in {output_dir}...")
    resolver = TargetResolver(output_dir, store)
    print(f"  {len(resolver)} media files indexed")

    # List zips
    if use_local:
        local_dir = Path(args.local_zips)
        if not local_dir.exists():
            print(f"ERROR: Local zip directory not found: {args.local_zips}")
            sys.exit(1)
        print(f"\nListing zips in {local_dir}...")
        zip_names = local_list_zips(local_dir)
    else:
        print(f"\nListing zips on phone ({args.phone_dir})...")
        try:
            zip_names = adb_list_zips(args.phone_dir)
        except AdbError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
    if not zip_names:
        print("  No zip files found!")
        sys.exit(1)
    for z in zip_names:
        print(f"  {z}")
    print(f"  {len(zip_names)} zip(s) total")

    # Stats
    overall_start = time.time()
    stats = {
        "fixed": 0,
        "already_done": 0,
        "no_json": 0,
        "no_output_match": 0,
        "no_metadata": 0,
        "errors": 0,
        "has_datetime": 0,
        "has_gps": 0,
        "has_description": 0,
        "has_people": 0,
        "dates_kept": 0,
        "unchanged": 0,
        "ambiguous": 0,
        "failed_before": 0,
        "no_result": 0,
    }
    failures: list[str] = []
    unfinished: list[str] = []

    try:
        for zi, zip_name in enumerate(zip_names, 1):
            reopen = options.retry_failed and store.has_failed(zip_name)
            if zip_name in store.completed_zips and not reopen:
                print(f"\n[{zi}/{len(zip_names)}] SKIP (already done): {zip_name}")
                continue

            print(f"\n{'=' * 65}")
            print(f"[{zi}/{len(zip_names)}] {zip_name}")
            print(f"{'=' * 65}")

            remote_zip = f"{args.phone_dir.rstrip('/')}/{zip_name}"
            zip_source = str(Path(args.local_zips) / zip_name) if use_local else remote_zip

            # Step 1: Get zip listing. A listing that fails must not look like an empty zip.
            print("  Listing zip contents...", end="", flush=True)
            t = time.time()
            try:
                if use_local:
                    entries = local_get_zip_listing(zip_source)
                else:
                    entries = adb_get_zip_listing(remote_zip)
                    if not entries:
                        raise OSError("the device returned no listing (disconnected, or unzip failed)")
            except (OSError, zipfile.BadZipFile) as exc:
                print(f" FAILED: {exc}")
                print("  This zip is left unfinished and will be tried again on the next run.")
                unfinished.append(zip_name)
                continue
            media_count = sum(1 for _, name in entries if is_media(name) and not name.endswith("/"))
            json_entries = [name for _, name in entries if name.endswith(".json")]
            print(f" {media_count} media, {len(json_entries)} JSON ({time.time()-t:.1f}s)")

            # Step 2: Read the JSON sidecars. Locally each one is read by name. Over ADB they
            # arrive as one stream (a single call instead of thousands), which is then cut at
            # the sizes from the listing.
            sized_json = [(size, name) for size, name in entries if name.endswith(".json")]
            print("  Streaming JSON metadata...", end="", flush=True)
            t = time.time()
            pairing = "name"
            read_sidecar: Callable[[str], bytes]
            if use_local:
                json_data_map = local_read_jsons(zip_source, json_entries)
                json_size = sum(size for size, _ in sized_json)
                read_sidecar = partial(_local_read_sidecar, zip_source)
            else:
                try:
                    raw_json_bytes = adb_stream_all_jsons(remote_zip)
                except AdbError as exc:
                    print(f" FAILED: {exc}")
                    print("  This zip is left unfinished and will be tried again on the next run.")
                    unfinished.append(zip_name)
                    continue
                json_size = len(raw_json_bytes)
                json_data_map, pairing = pair_json_stream(raw_json_bytes, sized_json)
                read_sidecar = partial(adb_stream_single_json, remote_zip)
            print(f" {format_size(json_size)} ({time.time()-t:.1f}s)")

            # Step 3: Report what could be parsed
            print(f"  Parsing JSON sidecars... {len(json_data_map)}/{len(json_entries)} parsed")
            if pairing == "none" and json_entries:
                print("  NOTE: the sidecar stream could not be matched to the listing, so each sidecar")
                print("        is read separately. This is slower but cannot mix sidecars up.")

            # Step 4: Process each media file
            zip_stats = fix_zip_entries(
                zip_name, entries, json_data_map, read_sidecar, resolver, store, options, stats
            )

            # A zip is finished once every entry has an outcome. Nothing is saved in a dry run,
            # otherwise a later real run would skip every entry the dry run "processed".
            if zip_stats["no_result"] == 0:
                if not options.dry_run:
                    store.complete_zip(zip_name)
            else:
                unfinished.append(zip_name)
            store.checkpoint()

            stats["fixed"] += zip_stats["fixed"]
            stats["unchanged"] += zip_stats["unchanged"]
            stats["no_json"] += zip_stats["no_json"]
            stats["no_output_match"] += zip_stats["no_match"]
            stats["no_metadata"] += zip_stats["no_meta"]
            stats["errors"] += zip_stats["err"]
            stats["already_done"] += zip_stats["skip"]
            for key in ("ambiguous", "failed_before", "no_result"):
                stats[key] += zip_stats[key]
            failures.extend(zip_stats["failures"])

            print(f"  DONE: {format_zip_summary(zip_stats)} ({format_time(zip_stats['elapsed'])})")
            sys.stdout.flush()
    finally:
        # Also reached on Ctrl-C: whatever finished is kept
        store.checkpoint()

    # Final summary
    elapsed = time.time() - overall_start
    print(f"\n{'=' * 65}")
    print(f"  METADATA FIX COMPLETE ({format_time(elapsed)})")
    print(f"{'=' * 65}")
    print(f"  Files with metadata fixed:  {stats['fixed']}")
    print(f"  Already correct, unchanged: {stats['unchanged']}")
    print(f"  Already processed (skip):   {stats['already_done']}")
    print(f"  No JSON sidecar found:      {stats['no_json']}")
    print(f"  No matching output file:    {stats['no_output_match']}")
    print(f"  No usable metadata in JSON: {stats['no_metadata']}")
    print(f"  Errors:                     {stats['errors']}")
    if stats["ambiguous"]:
        print(f"  Ambiguous file, skipped:    {stats['ambiguous']}")
    if stats["failed_before"]:
        print(f"  Failed in an earlier run:   {stats['failed_before']}  (retry with --retry-failed)")
    if stats["no_result"]:
        print(f"  No result, will be retried: {stats['no_result']}")
    print("  ---")
    print(f"  With date/time:             {stats['has_datetime']}")
    print(f"  With GPS coordinates:       {stats['has_gps']}")
    print(f"  With descriptions:          {stats['has_description']}")
    print(f"  With people tags:           {stats['has_people']}")
    print(f"  Dates kept (camera time):   {stats['dates_kept']}")
    print(f"{'=' * 65}")

    if failures:
        print(f"\n  ExifTool reported an error for {len(failures)} file(s):")
        for line in failures[:20]:
            print(f"    {line}")
        if len(failures) > 20:
            print(f"    ... and {len(failures) - 20} more")
        print("  They are remembered and skipped from now on; use --retry-failed to try them again.")
    if stats["ambiguous"]:
        print(f"\n  {stats['ambiguous']} file(s) share a name with another photo and could not be told apart.")
        print("  Nothing was written to them.")
    if unfinished:
        print(f"\n  {len(unfinished)} zip(s) were not finished and will be continued on the next run:")
        for name in unfinished:
            print(f"    {name}")

    if args.dry_run:
        print("\n  [DRY RUN] No files were modified.")
    elif not unfinished:
        print("\n  All done! Verify with:")
        print('  exiftool -DateTimeOriginal -CreateDate -GPSLatitude -GPSLongitude -XMP:PersonInImage "<file>"')


if __name__ == "__main__":
    main()
