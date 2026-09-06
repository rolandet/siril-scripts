# Design Decisions

## Keep README user-maintained

`README.md` belongs to the user. Codex should not overwrite it or replace its existing content. Repository orientation for future Codex sessions should live in `docs/repo-overview.md` and the rest of this `docs/` folder.

## Current active script

The current active development script is `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py`. Do not rename files or switch to another script unless the user explicitly asks.

`osc-multi-night-stacking-v2.1.py` and `osc-multi-night-with-mosiac-stacking-v2.2.py` are locked historical versions. Do not edit them unless the user explicitly asks for changes to those locked versions.

## Single-file application

The current app is intentionally a single-file PyQt6 application. Avoid broad refactors into packages or modules unless the user explicitly asks.

## Prefer generated Siril scripts

The project generates Siril `.ssf` processing scripts so workflows are repeatable across targets, sessions, nights, and mosaic panels.

## Keep Siril compatibility conservative

The project targets Siril 1.4.x. Treat command names, arguments, and flags as version-specific. Do not invent flags. Prefer known-good patterns already emitted by the script.

## Preserve known workflow behavior

Astrophotography processing changes can materially affect final image quality. Preserve behavior unless the requested change explicitly modifies it.

This is especially important for:

- Calibration precedence.
- Multi-night light-frame grouping.
- Registration and two-pass behavior.
- Drizzle behavior.
- Background extraction placement after calibration and before alignment.
- Mosaic pack-sequence disabling.
- Mosaic per-panel drizzle forcing two-pass behavior.
- Phase 2 mosaic drizzle being skipped.
- Normalization and stacking options.
- Final `mirrorx -bottomup` before save.
- Output filenames and working directories.

## Distortion correction defaults

Distortion correction is optional and persisted. New single-session projects default off, while new multi-session and mosaic projects default on. Once the user chooses a value or a project file supplies one, that explicit value is preserved. Legacy normal projects load with correction off; legacy mosaic projects load with correction on to preserve their previous generated behavior.

Normal multi-session projects solve the merged registration target once, immediately before global registration. Mosaic projects correct each source panel sequence during its initial registration and do not undistort already-corrected panel products a second time during cross-session or final channel alignment.

## Windows-first path handling

The user runs this workflow on Windows. Path handling should be checked with Windows-style paths, spaces, drive letters, and Siril's tolerance for POSIX-style slashes in generated scripts.

## Documentation and changelog

Update `CHANGELOG.md` for user-facing behavior changes. Update docs when a Siril command assumption changes or a risky workflow area is clarified.

## Prefer small changes

Codex should make small, reviewable changes with clear explanations. Large refactors should be avoided unless the user explicitly asks for them.

## Managed OSC mosaic storage

The low-disk builder compiles image commands from the existing mosaic generator instead of maintaining a separate calibration/registration algorithm. A collection-only switch bypasses legacy prepared-folder discovery and its mutating geometry preflight; the managed builder validates the selected FITS inputs and rejects mixed geometry without moving source files. Independent panels are then scheduled sequentially. The original per-night distortion registration and cross-night registration are both retained.

Cleanup is an explicit dependency operation. A run has a unique scratch directory and an ownership manifest; successful Siril commands, valid FITS payload extents, expected membership, retained output copies and atomic checkpoints precede release. Merge aliases are removed before their backing registered images. Directory reparse points and unowned/replaced entries are refused. Persistent file locks leave files in the live-space budget. Small selection/transform/WCS records are archived before image cleanup.

Generated workers use source captured when the application module loaded. Editing the script while its UI is open therefore cannot combine stale Python line numbers with newer source text during export. Siril 1.4.4's Python-exit-status and buffered-output behavior require one command controller, file-based Python diagnostics and a completion receipt in a unique directory for each invocation for exported SSF. The application runs the same controller in an API worker thread or through the CLI launcher, with no automatic fallback after managed processing starts.
