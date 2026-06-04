# Project History

## Background

This project grew out of an OSC multi-night stacking workflow for Siril 1.4.x. The goal is to make it easier to process astrophotography data across multiple imaging nights while preserving calibration, registration, normalization, drizzle, stacking, and output behavior.

The user works primarily on Windows and commonly processes data from NINA-style imaging sessions.

## Script naming

The current active development script is:

```text
osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py
```

`osc-multi-night-stacking-v2.1.py` and `osc-multi-night-with-mosiac-stacking-v2.2.py` are locked historical versions. They should be left unchanged unless the user explicitly asks to modify a locked version.

Older historical filenames may include:

- `osc-multi-night-stacking-1.0.py`
- `osc-multi-night-stacking-1.0.1.py`
- `osc-multi-night-stacking-1.1.py`
- `osc-multi-night-stacking-v1.2.py`
- `osc-multi-night-stacking-v2.0.py`
- `osc-multi-night-stacking-v2.0.x.py`

Future renaming should only be done when explicitly requested.

## Major workflow areas

- Single-file PyQt6 application structure.
- JSON project files.
- Multi-session OSC stacking.
- Session-level lights, bias, darks, flats, and dark-flats.
- Panel-level frame lists for mosaic mode.
- Per-session and per-panel calibration handling.
- Siril 1.4 `.ssf` command generation.
- Registration versus `seqapplyreg` behavior.
- Optional two-pass registration.
- Optional drizzle.
- Optional pack sequences for large non-mosaic projects.
- Experimental mosaic processing.
- Geometry preflight when `astropy` is available.
- In-Siril execution through `sirilpy`.
- `siril-cli` fallback and logging.
- Windows path handling.

## Important Siril lessons learned

- Siril `.ssf` scripts do not support arbitrary shell commands.
- `echo` caused a Siril script failure and must not be emitted.
- Do not invent Siril flags without validating them against Siril 1.4.
- Previous errors included unsupported parameters such as `-noout`.
- Maximize framing syntax must be treated carefully. The current script emits `seqapplyreg mosaic -framing=max` in mosaic Phase 2 when maximize framing is enabled.
- Siril command syntax should be treated as strict and version-specific.

## Current development direction

Future Codex work should be small, reviewable, and behavior-preserving unless the user asks for a change. For user-facing workflow changes, update `CHANGELOG.md` and the relevant docs.

Use `docs/repo-overview.md` for repository orientation. `README.md` is user-maintained and must not be overwritten.
