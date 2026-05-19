# Changelog

All notable user-facing changes to this project should be documented here.

## Unreleased

### Added

- Added Codex-facing documentation updates for the current `osc-multi-night-stacking-v2.1.py` architecture, features, Siril command assumptions, risks, and testing expectations.
- Added a non-mosaic `Background Extraction` option that emits `seqsubsky pp_light 1` after calibration and before alignment, then processes the resulting `bkg_pp_light` sequence.
- Added v3.0 narrowband extraction support in `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py` for Ha/OIII and SII/OIII dual-band OSC data, including mono Ha/SII/OIII stacks and SHO output with HOO fallback.
- Added Session and Panel editor filter tabs for `OSC`, `Ha/OIII`, and `SII/OIII` data entry.
- Added tooltips for the v3.0 processing tabs and filter-group tabs.
- Added v3.0 narrowband-mode broadband support: the `OSC` tab can now produce a companion `<project>_broadband_rgb.fit`, and an optional LRGB compose step can write `<project>_SHO_LRGB.fit` or `<project>_HOO_LRGB.fit`.

### Changed

- Clarified that `README.md` is user-maintained and must not be overwritten by Codex sessions.
- Clarified that the active script for current work is `osc-multi-night-stacking-v2.1.py`.
- Renamed the Drizzle UI group in the active v2.2 script to `Drizzle and Background Extraction`.
- Updated the v2.2 script's displayed version labels to `2.2` and renamed it to `osc-multi-night-with-mosiac-stacking-v2.2.py`.
- Reorganized the v3.0 left-side processing controls into `Registration & Stacking`, `Mosaic Processing`, and `Ha/SII and OIII Extraction` tabs while keeping Sessions, Panels, and run buttons visible.
- Narrowband mode now ignores drizzle settings and keeps extraction data in filter-specific working folders to avoid Siril sequence-name collisions.
- Expanded v3.0 Ha/OIII and SII/OIII filter tabs to include the same frame-list categories as OSC: lights, biases, darks, flats, and dark-flats.
- Added per-filter master override boxes for v3.0 Ha/OIII and SII/OIII tabs.
- Moved the v3.0 `Use 2-pass registration` and `Background Extraction` controls into the renamed `Registration and Stacking` box; the drizzle box is now labeled `Drizzle`.
- Clarified the v3.0 `OSC` tab behavior: with narrowband extraction disabled it remains the traditional OSC workflow; with extraction enabled it is treated as optional broadband/no-filter/UV-IR-cut data for RGB and luminance companion outputs.
- Defaulted the v3.0 OSC broadband and LRGB luminance options to off so broadband integration is explicit opt-in.
- Made `Save Ha, SII, and OIII mono stacks` toggleable; when off, only internal channel work files are kept for composition.
- Expanded the v3.0 narrowband output dropdown to support `SHO with HOO fallback`, forced `SHO`, and forced `HOO`.

### Fixed

### Notes

- This entry documents a documentation-only update. No Python code behavior was changed.
