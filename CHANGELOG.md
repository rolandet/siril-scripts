# Changelog

All notable user-facing changes to this project should be documented here.

## Unreleased

### Added

- Added Codex-facing documentation updates for the current `osc-multi-night-stacking-v2.1.py` architecture, features, Siril command assumptions, risks, and testing expectations.
- Added a non-mosaic `Background Extraction` option that emits `seqsubsky pp_light 1` after calibration and before alignment, then processes the resulting `bkg_pp_light` sequence.

### Changed

- Clarified that `README.md` is user-maintained and must not be overwritten by Codex sessions.
- Clarified that the active script for current work is `osc-multi-night-stacking-v2.1.py`.
- Renamed the Drizzle UI group in the active v2.2 script to `Drizzle and Background Extraction`.
- Updated the v2.2 script's displayed version labels to `2.2` and renamed it to `osc-multi-night-with-mosiac-stacking-v2.2.py`.

### Fixed

### Notes

- This entry documents a documentation-only update. No Python code behavior was changed.
