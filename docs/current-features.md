# Current Features - osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py

Source reviewed: `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py`

`osc-multi-night-stacking-v2.1.py` and `osc-multi-night-with-mosiac-stacking-v2.2.py` are locked historical versions. Do not edit them unless the user explicitly asks for changes to those locked versions.

## Summary

`osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py` is a single-file PyQt6 desktop application for building and running Siril 1.4 OSC multi-night stacking projects. It supports JSON projects, session-based and panel-based organization, Siril `.ssf` script generation, optional Siril Python API execution, `siril-cli` fallback execution, experimental mosaic processing, and optional Ha/OIII and SII/OIII narrowband extraction. When narrowband extraction is enabled, it generates the narrowband final instead of the normal OSC RGB final.

## Core project model

The project model stores:

- Project name, project JSON path, and working directory.
- Siril CLI path and force-CLI preference.
- Window-size persistence preferences.
- Inferred frame width and height.
- Global drizzle settings.
- Global background extraction setting.
- Global two-pass registration setting.
- Intermediate FITS compression setting.
- Optional 32-bit final stack output.
- Global stacking method and sigma rejection values.
- Pack-sequence mode and auto-pack threshold.
- Sessions.
- Mosaic settings.
- Narrowband extraction settings in the v3.0 branch: enabled flag, mono-output preference, NB channel-balancing mode, output palette, fixed `ha` resampling, fixed merged-OIII policy, and disabled NB drizzle policy.
- Whether uncalibrated runs are allowed.

## Session and panel models

Each session stores lights, bias, darks, flats, dark-flats, optional master bias/dark/flat/dark-flat overrides, an optional working subdirectory, and optional mosaic panels.

Each panel stores a panel ID, description, and its own lights, bias, darks, flats, and dark-flats. Panel-level calibration can override session-level calibration where applicable.

In the v3.0 branch, sessions and panels also store filter-specific `Ha/OIII` and `SII/OIII` frame sets. Each narrowband frame set has its own lights, bias, darks, flats, dark-flats, and optional master bias/dark/flat/dark-flat overrides; session/panel master overrides and the Siril Master Library remain available as fallbacks.

## Data preparation

The Prepare Working Directory action:

- Creates the working directory when needed.
- Builds per-session or per-panel folder structures.
- In v3.0 narrowband mode, links filter-specific frames under `ha_oiii` and `sii_oiii` subfolders to avoid sequence-name collisions during extraction.
- Links image files into the working tree.
- Attempts symlink first, then hardlink, then copy.
- Writes a timestamped `prepare_*.log`.
- Reports progress through Siril console integration when available.
- Can detect frame size from a FITS header and use it for linked mosaic feathering.
- Does not intentionally clear or change the project dirty state.

## Calibration selection and validation

Calibration frame priority is:

1. Panel override, when processing a mosaic panel.
2. Session override.
3. Project/library values when configured.
4. Siril Master Library variables when `Use Siril Master Library` is enabled.
5. No calibration frame.

The validator warns for missing darks, missing flats, and raw flats that lack bias or dark-flat support. The user can explicitly allow uncalibrated processing.

## Non-mosaic generated Siril pipeline

For standard multi-session processing, the generated `.ssf` script:

1. Emits `requires 1.4.0` and resets `setfindstar`.
2. Sets FITS compression with `setcompress`.
3. Converts raw bias, dark-flat, dark, flat, and light frames.
4. Builds session masters when raw calibration frames are supplied.
5. Calibrates OSC lights with `calibrate light -cfa`.
6. Uses `-cc=dark` when a dark is available.
7. Uses `-equalize_cfa` when flat calibration is used and drizzle is off.
8. Uses `-debayer` when drizzle is off.
9. Skips `-debayer` during calibration when drizzle is on.
10. Optionally runs `seqsubsky pp_light 1` on each calibrated session sequence before alignment.
11. Uses the `bkg_pp_light` sequence after background extraction.
12. Registers calibrated or background-subtracted light sequences.
13. Supports optional two-pass registration.
14. Supports drizzle through either direct registration or `seqapplyreg`, depending on drizzle and two-pass settings.
15. Merges multiple sessions into `all_sessions`.
16. Stacks the registered sequence.
17. Loads the final stack, runs `mirrorx -bottomup`, and saves `<project_slug>_final.fit`.

## Drizzle behavior

Global drizzle options include:

- Enable/disable drizzle.
- Scaling.
- Pixel fraction.
- Kernel: `square`, `point`, `turbo`, `gaussian`, `lanczos2`, or `lanczos3`.

When drizzle is enabled, light calibration does not debayer. Drizzle is applied during registration or sequence application. Mosaic per-panel drizzle also uses the same drizzle parameters.

## Background extraction behavior

The non-mosaic UI includes a `Background Extraction` checkbox in the `Registration and Stacking` box.

When enabled, generated `.ssf` scripts run:

```text
seqsubsky pp_light 1
```

This runs after light calibration and before registration/alignment. The resulting sequence uses Siril's `bkg_` prefix, so downstream registration uses `bkg_pp_light`.

In v3.0 narrowband mode, the same background extraction controls are reused after `seqextract_HaOIII`: extracted `Ha_...` and `OIII_...` sequences are background-subtracted before registration when the relevant checkbox is enabled.

## Narrowband extraction behavior in v3.0

When `Ha/SII and OIII Extraction` is enabled in `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py`, generated scripts:

- Calibrate narrowband lights as CFA and do not emit `-debayer`.
- Run `seqextract_HaOIII pp_light -resample=ha` for Ha/OIII and SII/OIII filter groups.
- Map Ha/OIII red extraction to Ha, SII/OIII red extraction to SII, and merge OIII extractions from both filters.
- Allow asymmetric filter coverage across sessions. Sessions with only one narrowband filter group contribute only that group's channels; for example, four SII/OIII sessions and two Ha/OIII sessions produce SII from four sessions, Ha from two sessions, and OIII from all contributing groups.
- Prefer filter-group master overrides over shared session/panel overrides, raw filter-group calibration frames, and Master Library variables.
- Disable drizzle for the narrowband path while preserving the user's drizzle settings for normal OSC processing.
- Register and stack mono Ha, SII, and OIII channels.
- Apply the selected `Final NB Framing` mode during final channel registration. The default `Common overlap (recommended)` emits `seqapplyreg nb_comp -framing=min` so SHO/HSO/HOO composition uses only the shared valid channel area; advanced options expose reference-frame, maximum-extent, and center-of-gravity framing.
- Balance the aligned mono channels before RGB composition with the `NB Channel Balancing` mode. The default `Median/MAD Match` aligns channel medians and MAD contrast, `Background Match Only` aligns channel medians while preserving channel contrast, and `None` preserves raw aligned channel levels. Pixel Math balancing intermediates are saved as 32-bit FITS before `rgbcomp`.
- Compose using the selected palette: `SHO with HOO fallback`, forced `SHO`, forced `HSO`, or forced `HOO`.
- Save the final composed image through the existing `mirrorx -bottomup` final-output step as `<project_slug>_<palette>_final.fit`, for example `<project_slug>_SHO_final.fit`.
- Store aggregate narrowband sequence scratch files under the first session's `nb_sequences` folder instead of the project root.

For mosaic narrowband projects, extraction happens before panel stacking. The script builds per-channel panel stacks, stitches Ha/SII/OIII channel mosaics independently, then composes the final narrowband RGB image.

The `OSC` tab has two roles:

- When narrowband extraction is disabled, it is the normal OSC workflow, including one traditional dual-band or broadband OSC dataset processed without Ha/SII/OIII channel extraction.
- When narrowband extraction is enabled, it can optionally be treated as broadband/no-filter/UV-IR-cut RGB data. This option is off by default; when enabled and OSC lights are present, v3.0 writes `<project_slug>_broadband_rgb.fit` in addition to the palette-named narrowband `<project_slug>_<palette>_final.fit`.

If `Create LRGB output from OSC luminance` is selected, the generated script aligns the broadband stack to the composed SHO/HSO/HOO image, splits the aligned broadband RGB image in Lab mode, uses the Lab L image as luminance, and writes an additional `<project_slug>_<palette>_LRGB.fit`. This does not replace the plain narrowband final.

The `Save Ha, SII, and OIII mono stacks` option controls whether named mono channel outputs such as `NB_Ha_mono.fit` are kept as user-facing products. Internal channel stack files are still created when the option is off because RGB composition needs aligned channel images.

## Stacking options

The UI exposes these stacking methods:

- Winsorized Rejection.
- Sigma Rejection.
- Mean.
- Median.

Generated stack methods map through `map_stack_method()`:

- Winsorized rejection: `stack ... rej <low> <high>`.
- Sigma rejection: `stack ... rej sigma <low> <high>`.
- Mean: `stack ... mean none`.
- Median: `stack ... med`.

Final stacks can optionally include `-32b`.

## Pack sequence support

Pack-sequence modes include:

- Off.
- FITSEQ.
- SER.
- Auto when above a threshold.

Auto mode defaults to 2000 frames. Packing applies to light conversion in non-mosaic mode. Mosaic mode intentionally forces packing off because Siril 1.4 cannot plate-solve packed FITSEQ/SER sequences in this workflow.

## Mosaic mode

Mosaic mode is marked experimental. It supports:

- Grid layout rows and columns.
- Overlap percentage.
- Global reference selection.
- Canvas scale.
- One-pass or two-pass mosaic registration.
- Panel background extraction.
- Maximize framing.
- Normalize on overlaps.
- Border feathering in pixels.
- Optional link between overlap percentage and feathering.
- Drizzle per panel.
- Auto-manage panels by grid.
- Panel name schemes: `A1`, `B2`, etc., or `R1C2`.
- Preview mosaic layout dialog.
- Copy calibration frames from the first panel to other panels.

## Mosaic generated Siril pipeline

Mosaic processing is split into phases:

1. Per-panel conversion, calibration, optional background extraction, WCS solving, and registration.
2. Per-panel cross-session merge and stack.
3. Phase 2 stitching from panel final FITS files into a mosaic sequence.
4. WCS plate solving of the mosaic sequence.
5. Optional max-framing sequence application.
6. Mosaic stacking with optional maximize, feathering, and overlap normalization.
7. Final `mirrorx -bottomup` and save to `<project_slug>_final.fit`.

Phase 2 deliberately skips drizzle because it uses RGB panel final images rather than mono/CFA sequences.

Drizzle per panel intentionally forces two-pass behavior because drizzle output is generated through `seqapplyreg`.

## Geometry preflight

When `astropy` is available, the script can:

- Read FITS dimensions from converted light files.
- Detect the modal light-frame geometry.
- Move mismatched converted light frames into `_mismatch_geometry`.
- Warn if master dark or flat geometry does not match the modal light geometry.

If `astropy` is not available, geometry preflight is skipped.

## Running Siril

The Run action:

- Requires `run_project.ssf` to exist.
- Validates calibration coverage again before running.
- Prefers in-Siril execution through `sirilpy` when available.
- Can force use of `siril-cli`.
- Falls back to `siril-cli` if the Python API run fails.
- Logs CLI output to timestamped `siril_run_*.log`.
- Streams CLI output back to the Siril console when connected.
- Tracks run mode in the GUI.
- Loads the final image into Siril after a successful CLI run when connected.

Abort behavior is intended for CLI runs. On Windows it sends `CTRL_BREAK_EVENT`; on other platforms it sends `SIGINT`. In-Siril API runs should be stopped from Siril itself.

## Known implementation notes to preserve

- Siril `.ssf` output must not include shell commands such as `echo`.
- Non-mosaic background extraction intentionally starts with the basic `seqsubsky pp_light 1` command form.
- Mosaic mode intentionally forces pack sequence mode off.
- Drizzle per panel intentionally forces two-pass behavior.
- Phase 2 mosaic drizzle is intentionally skipped.
- The final image is intentionally mirrored with `mirrorx -bottomup` before saving.
- The script currently uses a single-file application structure.
- Build and run operations intentionally preserve dirty-state behavior where noted in code.
- Current mosaic canvas scaling saves `mosaic_final_scaled.fit`, but final promotion still loads `mosaic_final.fit`; treat this as existing behavior unless changing it explicitly.
- The About dialog text appears to contain a typo: `winorized` instead of `winsorized`.
- There is duplicated initial window sizing/centering code in `MainWindow.__init__`; this is harmless but could be cleaned later.
