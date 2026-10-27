# Changelog

All notable changes to this project are documented in this file. Entries follow the
[GitHub releases](https://github.com/muhib-karim/google-takeout-metadata-fixer/releases) of this repository.

## [Unreleased]

- Changes on `main` since v1.0.0: see the [comparison](https://github.com/muhib-karim/google-takeout-metadata-fixer/compare/v1.0.0...main).

## [1.0.0] - 2026-03-12

Initial public release ([release page](https://github.com/muhib-karim/google-takeout-metadata-fixer/releases/tag/v1.0.0)).

### Added

- Restores EXIF dates, GPS, descriptions, people tags and video timestamps from Google Takeout JSON sidecars.
- Works on local Takeout zips and on Android over ADB.
- Matches supplemental-metadata names, truncated names, duplicates, edited-file variants and live photos.
- Resumable ADB copy helper (`adb_resumable_copy.py`) and a recovery helper for zips with a broken listing (`recover_takeout_zip_metadata.py`).
- Project documentation and branding.
