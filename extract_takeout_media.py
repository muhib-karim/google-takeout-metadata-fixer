"""
Google Takeout Processor - Process takeout zips directly from phone

Reads zip files from your phone (via drive path or ADB), extracts and fixes
metadata one file at a time to minimize HDD usage. Keeps the zips on the phone.

Usage:
  # Phone mounted as drive letter (or zips on USB stick, etc.)
    python extract_takeout_media.py E:\\Takeout

  # Android phone via ADB (no drive letter needed)
    python extract_takeout_media.py --adb /sdcard/Takeout

  # Custom output directory
    python extract_takeout_media.py --adb /sdcard/Takeout --output fixed_photos

Disk usage:
  - Direct mode: only 1 media file at a time + output folder
  - ADB mode: 1 zip at a time + 1 media file + output folder
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from google_takeout_metadata_fixer import (  # noqa: E402
    SidecarIndex,
    _loads_object,
    apply_metadata_batch,
    build_exiftool_args,
    find_json_match,
    is_media,
    parse_takeout_json,
)

OUTPUT_DIR = "output"
PROGRESS_FILE = "processing_progress.json"
TEMP_ZIP_DIR = "_temp_zip"
STAGING_PREFIX = ".gtmf-staging-"


def _journal_path():
    return PROGRESS_FILE + ".journal"


def load_progress():
    """Load the progress file, then replay the journal of entries finished since it was saved."""
    progress: dict[str, Any] = {"completed_zips": [], "processed_files": {}}
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                progress.update(loaded)
        except (OSError, ValueError) as exc:
            print(f"WARNING: {PROGRESS_FILE} could not be read ({exc}); starting with empty progress.")
    if not isinstance(progress.get("processed_files"), dict):
        progress["processed_files"] = {}
    if not isinstance(progress.get("completed_zips"), list):
        progress["completed_zips"] = []
    try:
        with open(_journal_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    entry_key, name = json.loads(line)
                    progress["processed_files"][entry_key] = name
                except (ValueError, TypeError):
                    continue  # a line cut short by a crash
    except OSError:
        pass
    return progress


def record_processed(progress, entry_key, output_name):
    """Remember that a zip entry has been extracted, immediately and cheaply.

    Rewriting the whole progress file for every photo would be far too slow, so each
    finished entry is appended to a journal. Without this a re-run after an interrupt
    extracted the same photos again under new names (photo_1.jpg).
    """
    progress.setdefault("processed_files", {})[entry_key] = output_name
    with open(_journal_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps([entry_key, output_name]) + "\n")


def save_progress(progress):
    for _attempt in range(5):
        try:
            tmp = PROGRESS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(progress, f, indent=2)
            os.replace(tmp, PROGRESS_FILE)
            break
        except PermissionError:
            time.sleep(1)
    else:
        # Last resort: just write directly
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)
    try:
        os.remove(_journal_path())  # everything in it is now in the progress file
    except OSError:
        pass


def find_json_in_zip(nameset, media_entry):
    """Find the JSON sidecar for a media file within a zip's namelist.

    Kept for compatibility; matching is done by the shared matcher in
    google_takeout_metadata_fixer.py.
    """
    return find_json_match(nameset, media_entry)


# --- Output path handling ---

def get_unique_output_path(output_dir, filename):
    """Get a unique output path, adding _1, _2 etc. if file exists."""
    output_file = output_dir / filename
    if not output_file.exists():
        return output_file

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while output_file.exists():
        output_file = output_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return output_file


# --- ADB helpers ---

def check_adb():
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split("\n")[1:] if l.strip()]
        devices = [l for l in lines if "device" in l]
        if not devices:
            print("ERROR: No Android device found. Connect your phone and enable USB debugging.")
            sys.exit(1)
        return True
    except FileNotFoundError:
        print("ERROR: ADB not found! Install Android SDK Platform Tools and add to PATH.")
        print("  https://developer.android.com/tools/releases/platform-tools")
        sys.exit(1)


def adb_list_zips(remote_dir):
    """List zip files on the Android device."""
    result = subprocess.run(
        ["adb", "shell", "ls", "-1", remote_dir],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"ADB error listing {remote_dir}: {result.stderr.strip()}")
        sys.exit(1)
    files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    return sorted([f for f in files if f.lower().endswith(".zip")])


def adb_get_file_size(remote_path):
    """Get file size on Android device in bytes."""
    r = subprocess.run(
        ["adb", "shell", "stat", "-c", "%s", remote_path],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return 0


def format_size(nbytes):
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def format_time(seconds):
    """Human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def adb_pull(remote_path, local_path):
    """Pull a file from Android device with live progress."""
    name = Path(remote_path).name
    total_size = adb_get_file_size(remote_path)
    size_str = format_size(total_size) if total_size else "unknown size"
    print(f"  Pulling: {name} ({size_str})")
    sys.stdout.flush()

    start = time.time()
    # Stream adb pull output live (progress goes to stderr)
    proc = subprocess.Popen(
        ["adb", "pull", remote_path, str(local_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # Monitor file size growth for progress
    local_file = Path(local_path)
    while proc.poll() is None:
        time.sleep(2)
        if local_file.exists():
            current = local_file.stat().st_size
            elapsed = time.time() - start
            speed = current / elapsed if elapsed > 0 else 0
            if total_size > 0:
                pct = current / total_size * 100
                eta = (total_size - current) / speed if speed > 0 else 0
                print(f"    {format_size(current)}/{size_str} ({pct:.1f}%) | {format_size(speed)}/s | ETA: {format_time(eta)}   ", end="\r")
            else:
                print(f"    {format_size(current)} | {format_size(speed)}/s | {format_time(elapsed)}   ", end="\r")
            sys.stdout.flush()

    stdout, stderr = proc.communicate()
    elapsed = time.time() - start
    speed = total_size / elapsed if elapsed > 0 and total_size > 0 else 0

    if proc.returncode != 0:
        print(f"\n  ADB pull FAILED: {stderr.decode().strip()}")
        return False

    print(f"    Pulled {size_str} in {format_time(elapsed)} ({format_size(speed)}/s)                    ")
    sys.stdout.flush()
    return True


# --- Core processing ---

def process_zip(zip_path, output_dir, progress, zip_index=0, total_zips=0):
    """Extract all media files of one takeout zip, one file at a time, and write their metadata.

    Returns True when every media entry was extracted, which is when the zip may be
    recorded as completed.
    """
    zip_name = Path(zip_path).name
    zip_label = f"[Zip {zip_index}/{total_zips}]" if total_zips else ""

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"  ERROR: {zip_name} cannot be read as a zip file ({exc}); it is left unfinished")
        return False

    with zf:
        all_names = zf.namelist()
        sidecar_index = SidecarIndex(all_names)
        media_entries = [e for e in all_names if is_media(e) and not e.endswith("/")]

        # Calculate total uncompressed media size
        media_set = set(media_entries)
        media_size_total = sum(info.file_size for info in zf.infolist() if info.filename in media_set)
        print(f"  {len(media_entries)} media files ({format_size(media_size_total)}) in this zip")
        sys.stdout.flush()

        fixed = 0
        copied = 0
        errors = 0
        skipped = 0
        not_written = 0
        bytes_done = 0
        zip_start = time.time()

        for i, entry in enumerate(media_entries, 1):
            filename = Path(entry).name
            entry_key = f"{zip_name}::{entry}"

            # Get file size from zip info
            try:
                file_size = zf.getinfo(entry).file_size
            except KeyError:
                file_size = 0

            # Skip already processed
            if entry_key in progress.get("processed_files", {}):
                skipped += 1
                bytes_done += file_size
                continue

            temp_dir = None
            try:
                # Stage the file inside the output folder. The last step is then a rename
                # on the same file system, which happens completely or not at all, so an
                # interrupt never leaves a half-written or unrecorded file in the output.
                temp_dir = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=output_dir))
                temp_media = temp_dir / filename

                with zf.open(entry) as src, open(temp_media, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                # Find and parse the JSON sidecar. A sidecar that is missing or cannot be
                # used must never cost the photo itself.
                metadata = {}
                json_entry = sidecar_index.match(entry)
                if json_entry:
                    try:
                        metadata = parse_takeout_json(_loads_object(zf.read(json_entry)))
                    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
                        metadata = {}

                # Apply metadata with exiftool if we have any, before the file is published
                outcome = "copied"
                if metadata and build_exiftool_args(metadata, temp_media):
                    result = apply_metadata_batch([(temp_media, metadata)])[0]
                    if result.status in ("written", "kept"):
                        outcome = "fixed"
                    else:
                        outcome = "not_written"
                        print(f"    exiftool could not write {filename}: {result.message}")

                # Publish under a unique name and record it
                output_file = get_unique_output_path(output_dir, filename)
                os.replace(temp_media, output_file)
                record_processed(progress, entry_key, output_file.name)

                fixed += outcome == "fixed"
                copied += outcome == "copied"
                not_written += outcome == "not_written"
                bytes_done += file_size

                # Show live progress every 10 files or at end
                if i % 10 == 0 or i == len(media_entries):
                    elapsed = time.time() - zip_start
                    rate = bytes_done / elapsed if elapsed > 0 else 0
                    pct = bytes_done / media_size_total * 100 if media_size_total > 0 else 0
                    eta = (media_size_total - bytes_done) / rate if rate > 0 else 0
                    print(
                        f"  {zip_label} [{i}/{len(media_entries)}] {pct:.0f}% "
                        f"| {format_size(bytes_done)}/{format_size(media_size_total)} "
                        f"| {format_size(rate)}/s | ETA: {format_time(eta)} "
                        f"| fix={fixed} copy={copied} err={errors}"
                    )
                    sys.stdout.flush()

            except Exception as e:
                errors += 1
                bytes_done += file_size
                print(f"    ERROR: {filename}: {e}")
                sys.stdout.flush()
            finally:
                if temp_dir and temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)

        save_progress(progress)
        elapsed = time.time() - zip_start
        summary = f"  DONE {zip_name}: {fixed} fixed, {copied} no-metadata, {errors} errors, {skipped} skipped"
        if not_written:
            summary += f", {not_written} metadata not written"
        print(f"{summary} ({format_time(elapsed)})")
        if errors:
            print("  Files with errors were not extracted; this zip is left unfinished and is retried on the next run.")
        sys.stdout.flush()
        return errors == 0


def main():
    parser = argparse.ArgumentParser(
        description="Process Google Takeout zips from phone, minimizing disk usage"
    )
    parser.add_argument(
        "source",
        help="Path to folder with takeout zips (drive letter or Android path with --adb)"
    )
    parser.add_argument(
        "--adb", action="store_true",
        help="Use ADB to pull zips from Android phone one at a time"
    )
    parser.add_argument(
        "--output", default=OUTPUT_DIR,
        help=f"Output directory for fixed photos (default: {OUTPUT_DIR})"
    )

    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Staging folders left behind by a run that was killed hold unfinished files only
    for stale in output_dir.glob(STAGING_PREFIX + "*"):
        shutil.rmtree(stale, ignore_errors=True)

    # Check exiftool
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True, timeout=10)
    except FileNotFoundError:
        print("ERROR: ExifTool not found! Install it and add to PATH.")
        print("  https://exiftool.org/install.html")
        sys.exit(1)

    progress = load_progress()

    if args.adb:
        # ==================== ADB MODE ====================
        check_adb()
        print("=== ADB Mode ===")
        print(f"Source:  phone:{args.source}")
        print(f"Output:  {output_dir.resolve()}\n")

        zip_names = adb_list_zips(args.source)
        if not zip_names:
            print(f"No zip files found at {args.source}")
            sys.exit(1)

        # Get sizes from phone
        zip_sizes = {}
        total_phone_size = 0
        for zn in zip_names:
            rp = f"{args.source.rstrip('/')}/{zn}"
            sz = adb_get_file_size(rp)
            zip_sizes[zn] = sz
            total_phone_size += sz

        print(f"Found {len(zip_names)} zip files on phone ({format_size(total_phone_size)} total):")
        for zn in zip_names:
            done = "DONE" if zn in progress.get("completed_zips", []) else "    "
            print(f"  {done} {zn}  ({format_size(zip_sizes[zn])})")

        # Check disk space
        import shutil as _shutil
        disk = _shutil.disk_usage(str(output_dir.resolve()))
        print(f"\nDisk free: {format_size(disk.free)}")
        print(f"Largest zip: {format_size(max(zip_sizes.values()))}")
        print("Needed per cycle: largest zip + output files\n")

        overall_start = time.time()
        temp_zip_dir = Path(TEMP_ZIP_DIR)
        temp_zip_dir.mkdir(exist_ok=True)

        for zi, zip_name in enumerate(zip_names, 1):
            if zip_name in progress.get("completed_zips", []):
                print(f"\n[{zi}/{len(zip_names)}] SKIP (already done): {zip_name}")
                continue

            print(f"\n{'='*60}")
            print(f"[{zi}/{len(zip_names)}] {zip_name} ({format_size(zip_sizes[zip_name])})")
            print(f"{'='*60}")

            remote_path = f"{args.source.rstrip('/')}/{zip_name}"
            local_zip = temp_zip_dir / zip_name

            if not adb_pull(remote_path, local_zip):
                continue

            try:
                if process_zip(local_zip, output_dir, progress, zi, len(zip_names)):
                    progress.setdefault("completed_zips", []).append(zip_name)
                save_progress(progress)
            finally:
                # Free HDD space immediately
                if local_zip.exists():
                    local_zip.unlink()
                    print(f"  Cleaned up local copy of {zip_name}")
                # Show overall progress
                done_zips = len(progress.get("completed_zips", []))
                done_files = len(progress.get("processed_files", {}))
                elapsed = time.time() - overall_start
                print(f"  Overall: {done_zips}/{len(zip_names)} zips, {done_files} files, elapsed: {format_time(elapsed)}")
                disk = _shutil.disk_usage(str(output_dir.resolve()))
                print(f"  Disk free: {format_size(disk.free)}")
                sys.stdout.flush()

        # Clean up temp dir
        if temp_zip_dir.exists() and not list(temp_zip_dir.iterdir()):
            temp_zip_dir.rmdir()

    else:
        # ==================== DIRECT PATH MODE ====================
        source_dir = Path(args.source)

        if not source_dir.exists():
            print(f"ERROR: '{args.source}' not found!")
            print()
            print("If your phone uses MTP (no drive letter), try one of these:")
            print("  1. Use ADB mode:  python extract_takeout_media.py --adb /sdcard/Takeout")
            print("  2. Map phone as drive letter (use an app like 'USB Mass Storage Enabler')")
            print("  3. Share phone folder over WiFi and use the network path")
            sys.exit(1)

        zip_files = sorted(source_dir.glob("*.zip"))
        if not zip_files:
            print(f"No zip files found in {args.source}")
            sys.exit(1)

        print("=== Direct Mode ===")
        print(f"Source:  {source_dir.resolve()}")
        print(f"Output:  {output_dir.resolve()}")
        print(f"Zips:    {len(zip_files)}\n")

        for zi, zip_path in enumerate(zip_files, 1):
            zip_name = zip_path.name

            if zip_name in progress.get("completed_zips", []):
                print(f"[{zi}/{len(zip_files)}] SKIP (done): {zip_name}")
                continue

            print(f"[{zi}/{len(zip_files)}] {zip_name}")
            if process_zip(zip_path, output_dir, progress, zi, len(zip_files)):
                progress.setdefault("completed_zips", []).append(zip_name)
            save_progress(progress)
            print()

    total_files = len(progress.get("processed_files", {}))
    total_zips = len(progress.get("completed_zips", []))
    print("\n=== All Done ===")
    print(f"Processed {total_files} media files from {total_zips} zips")
    print(f"Output: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
