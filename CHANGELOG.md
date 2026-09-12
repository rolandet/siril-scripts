# Changelog

All notable user-facing changes to this project should be documented here.

## Unreleased

### Added

- Created `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py` for the storage improvements; preserved v3.0 unchanged.
- Added opt-in `Low disk usage` for normal OSC mosaics on Siril 1.4.4: finish each panel across its nights, validate outputs, then remove run-owned temporary sequences. Both registration stages and all image-processing settings are retained.
- Added within-run, within-session reuse of identical flat calculations, isolated scratch bundles, source/master fingerprints, atomic checkpoints, completed-panel recovery, and free-space checks with a configurable reserve (20 GiB by default).
- Added explicit uncompressed / lossless GZIP2 intermediate choices for low-disk runs. GZIP2 uses quantization zero and `.fit.fz` files; final stacks remain uncompressed `.fit`.
- Added managed API/CLI execution with checked command completion, cancellation, persistent logs, and standalone exported workers. Old projects retain `Keep intermediates`; non-mosaic and narrowband processing retain their existing builders.

- Added Codex-facing documentation updates for the current `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py` architecture, features, Siril command assumptions, risks, and testing expectations.
- Added a non-mosaic `Background Extraction` option that emits `seqsubsky pp_light 1` after calibration and before alignment, then processes the resulting `bkg_pp_light` sequence.
- Added v3.0 narrowband extraction support in `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py` for Ha/OIII and SII/OIII dual-band OSC data, including mono Ha/SII/OIII stacks and SHO output with HOO fallback.
- Added Session and Panel editor filter tabs for `OSC`, `Ha/OIII`, and `SII/OIII` data entry.
- Added tooltips for the v3.0 processing tabs and filter-group tabs.
- Added v3.0 narrowband-mode broadband support: the `OSC` tab can now produce a companion `<project>_broadband_rgb.fit`, and an optional LRGB compose step can write `<project>_<palette>_LRGB.fit`.
- Added a v3.0 `NB Channel Balancing` selector with `Median/MAD Match` as the default, plus `Background Match Only` and `None` options for narrowband RGB composition.
- Added a v3.0 `Final NB Framing` selector for final Ha/SII/OIII channel registration, defaulting to common-overlap framing.
- Added a v3.0 `OIII Combine Policy` selector with the current merge-all behavior as the default, plus advanced auto-weighted and manual-weighted blends of separately stacked Ha/OIII-derived and SII/OIII-derived OIII masters.
- Added a read-only `OIII Weights` display showing estimated Ha/OIII vs SII/OIII blend percentages from the project light lists.
- Added `GESDT Rejection` as a global v3.0 stacking option, with dedicated outlier-fraction and significance controls that emit Siril 1.4.4 `stack ... rej generalized <outliers> <significance>` commands.
- Added a persisted `Distortion Correction (plate solve + registration)` option for normal OSC, narrowband, and mosaic registration. New single-night projects default off; new multi-night and mosaic projects default on; an explicit user choice always wins.
- Added generated-script coverage for enabled/disabled single-session, multi-session, and mosaic distortion-correction workflows, including legacy project migration and packed-sequence compatibility.

### Changed

- Renamed the legacy compression checkbox to `Compress Intermediates (Siril settings)`: its existing behavior uses Siril preferences and does not itself guarantee lossless floating-point compression.

- Changed v3.0 mosaic feathering to default to `Auto-calculate feathering from Overlap %`, matching the overlap-based planning model used by N.I.N.A. The pixel field is read-only in automatic mode and now displays the calculation and source frame geometry.
- Automatic mosaic feathering now reads representative light-frame FITS headers as soon as project data is available, without requiring `Prepare Working Directory`. Mixed frame sizes are reported and use the smallest short edge conservatively.
- Clarified that `README.md` is user-maintained and must not be overwritten by Codex sessions.
- Clarified that the active development script is `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py`, and that v2.1/v2.2 scripts are locked historical versions.
- Renamed the Drizzle UI group in the active v2.2 script to `Drizzle and Background Extraction`.
- Updated the v2.2 script's displayed version labels to `2.2` and renamed it to `osc-multi-night-with-mosiac-stacking-v2.2.py`.
- Reorganized the v3.0 left-side processing controls into `Registration and Stacking`, `Mosaic Processing`, and `Ha/SII and OIII Extraction` tabs while keeping Sessions, Panels, and run buttons visible.
- Narrowband mode now ignores drizzle settings and keeps extraction data in filter-specific working folders to avoid Siril sequence-name collisions.
- Expanded v3.0 Ha/OIII and SII/OIII filter tabs to include the same frame-list categories as OSC: lights, biases, darks, flats, and dark-flats.
- Added per-filter master override boxes for v3.0 Ha/OIII and SII/OIII tabs.
- Moved the v3.0 `Use 2-pass registration` and `Background Extraction` controls into the renamed `Registration and Stacking` box; the drizzle box is now labeled `Drizzle`.
- Clarified the v3.0 `OSC` tab behavior: with narrowband extraction disabled it remains the traditional OSC workflow; with extraction enabled it is treated as optional broadband/no-filter/UV-IR-cut data for RGB and luminance companion outputs.
- Defaulted the v3.0 OSC broadband and LRGB luminance options to off so broadband integration is explicit opt-in.
- Made `Save Ha, SII, and OIII mono stacks` toggleable; when off, only internal channel work files are kept for composition.
- Expanded the v3.0 narrowband output dropdown to support `SHO with HOO fallback`, forced `SHO`, forced `HSO`, and forced `HOO`.
- Replaced the v3.0 narrowband channel-normalization checkbox with the `NB Channel Balancing` selector. Existing projects with normalization enabled load as `Median/MAD Match`; existing projects with normalization disabled load as `None`.
- Narrowband final FITS filenames now include the resolved palette label, such as `<project>_SHO_final.fit`, `<project>_HOO_final.fit`, or `<project>_HSO_final.fit`.
- Narrowband RGB composition now defaults final channel registration to `seqapplyreg nb_comp -framing=min`, preventing blank registration borders from being fed into channel balancing and `rgbcomp`.
- Advanced weighted OIII blend modes now align source-specific OIII masters, optionally match them onto a common scale, and blend them with either OIII sub-count weights or the manual Ha/OIII vs SII/OIII slider.
- Weighted OIII blend source matching now follows `NB Channel Balancing`: Median/MAD match, background-only match, or no source matching when set to `None`.
- In v3.0, removing a session now renumbers remaining sessions to `Session 1..N`, remaps mosaic session references, and renames default session working folders when possible.
- In v3.0, removing the final remaining session now runs the normal deletion flow, then recreates an empty `Session 1` with a clear dialog.
- v3.0 mosaic sessions can again remove the final panel, leaving the session with no panels until a new one is added.
- Normal multi-session distortion correction now plate-solves `all_sessions_00001` after merge and applies `register ... -disto=file platesolve_data.wcs`; single-session workflows solve their selected calibrated/background-extracted registration sequence.
- Mosaic distortion correction remains enabled by default but can now be disabled for troubleshooting. Phase 2 WCS plate-solving for panel stitching remains independent and is still emitted when multiple panel finals are present.
- Sequence packing is forced off while distortion correction is enabled because the Siril 1.4 plate-solving workflow requires unpacked FITS sequences.

### Fixed

- Restored Siril's current working directory to the project root after low-disk managed runs instead of leaving the receipt directory active.
- Added total elapsed-time reporting to low-disk Siril API runs. The duration is now persisted in `state.json` and `run.log`, written to the Siril log, and shown in the completion dialog.
- Updated the `Prepare Working Directory` tooltip to describe on-demand input aliases and scratch folders when `Low disk usage` is selected, while retaining the immediate per-session preparation description for `Keep intermediates`.
- Fixed `Remove Data (All Sessions)` leaving the status bar showing `Project has unsaved changes.` when the project was clean. The cleanup now restores both the dirty flag and its visible status.
- Fixed v3.0 linked mosaic feathering silently retaining a stale pixel value after reopening a project. Script generation now recalculates automatic feathering and stops with a clear warning when no readable light-frame geometry is available.
- Fixed automatic mosaic feathering for FPACK tile-compressed `.fit.fz`/`.fits.fz` lights by reading `ZNAXIS1/ZNAXIS2` from compressed-image extensions without decompressing image data.
- Fixed automatic mosaic feathering stopping at an unreadable first light; geometry discovery now tries later configured lights for that panel or session.
- Fixed GESDT command generation so it uses Siril's valid `0.3` outlier-fraction and `0.05` significance defaults instead of incorrectly passing the `3.0` Sigma Low/High defaults, which caused `stack` to fail with `invalid arguments`.
- Refined v3.0 narrowband validation so explicit `HOO` output does not warn about missing SII/OIII data, while `SHO with HOO fallback` reports the fallback during script build without repeating the notice when the built script is run. Forced `SHO` and `HSO` still require SII/OIII lights.
- Fixed v3.0 narrowband helper stacking so the `Sigma Rejection` UI selection emits Siril sigma rejection commands instead of falling back to winsorized rejection.
- Fixed v3.0 HOO narrowband composition metadata by adding `rgbcomp -nosum` when OIII is reused for both green and blue, avoiding double-counted FITS exposure/stack keywords.
- Fixed v3.0 NB Channel Balancing Pixel Math intermediates so `Median/MAD Match` and `Background Match Only` save their temporary balanced channel files as 32-bit FITS instead of allowing Siril to quantize them to 16-bit.
- Moved v3.0 narrowband aggregate sequence scratch files into `Session 1/nb_sequences` instead of writing them into the project root.

### Notes

- The v3.0 session and panel safeguards are UI-only and do not change generated Siril command syntax.
