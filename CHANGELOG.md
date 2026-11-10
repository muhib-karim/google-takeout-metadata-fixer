# Changelog

All notable changes to this project are documented in this file. Entries follow the
[GitHub releases](https://github.com/muhib-karim/google-takeout-metadata-fixer/releases) of this repository.

## [Unreleased]

Changes on `main` since v1.0.0: see the [comparison](https://github.com/muhib-karim/google-takeout-metadata-fixer/compare/v1.0.0...main).

### Security

- Sidecar text is escaped before it is passed to ExifTool. A multi-line description used to be split into several arguments, so its later lines were read as file names and options.
- File paths are passed to ExifTool as absolute paths, so a file name starting with `-` or `#` is no longer read as an option or a comment.

### Fixed

- GPS: southern and western positions were written as northern and eastern ones on MP4, MOV and GIF; altitudes below sea level were written as above.
- Dates: a photo's local capture time was overwritten with UTC while its old offset tag stayed, shifting the photo by its time zone. Dates before 1970 were replaced by the upload time. One video date tag depended on the time zone of the machine running the tool.
- Sidecar matching: old-format duplicates (`photo(1).jpg` with `photo.jpg(1).json`), truncated names combined with a duplicate index, names cut inside a long media name, edited copies and live videos of duplicates, and a title fallback that crossed album folders. Results no longer depend on the hash seed, and matching a large old-format zip takes under a second instead of minutes.
- Sidecar contents were paired with sidecar names by position; one empty sidecar shifted the metadata of every following file onto its neighbour.
- Two different photos with the same file name in different albums received each other's metadata.
- Resume: failed and timed-out writes, and zips whose listing failed, were recorded as completed. Progress was only saved once per zip. A damaged progress file ended the run with a traceback.
- A sidecar field with an unexpected type or value aborted the whole run.

### Added

- `--overwrite-dates` and `--retry-failed`.
- A photo's embedded capture time is kept when it is consistent with the sidecar.
- Per-file results from ExifTool, a progress journal, and a summary of failed and ambiguous files.

### Changed

- Python 3.10 or newer is required; CI tests 3.10 to 3.15, with mypy, a secret scan and a dependency audit.
- The test suite no longer needs Pillow.

## [1.0.0] - 2026-03-12

Initial public release ([release page](https://github.com/muhib-karim/google-takeout-metadata-fixer/releases/tag/v1.0.0)).

### Added

- Restores EXIF dates, GPS, descriptions, people tags and video timestamps from Google Takeout JSON sidecars.
- Works on local Takeout zips and on Android over ADB.
- Matches supplemental-metadata names, truncated names, duplicates, edited-file variants and live photos.
- Resumable ADB copy helper (`adb_resumable_copy.py`) and a recovery helper for zips with a broken listing (`recover_takeout_zip_metadata.py`).
- Project documentation and branding.
