# Design Decisions

## Keep README user-maintained

`README.md` belongs to the user. Codex should not overwrite it or replace its existing content. Repository orientation for future Codex sessions should live in `docs/repo-overview.md` and the rest of this `docs/` folder.

## Current active script

The current main script for active work is `osc-multi-night-stacking-v2.1.py`. Do not rename files or switch to another script unless the user explicitly asks.

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

## Windows-first path handling

The user runs this workflow on Windows. Path handling should be checked with Windows-style paths, spaces, drive letters, and Siril's tolerance for POSIX-style slashes in generated scripts.

## Documentation and changelog

Update `CHANGELOG.md` for user-facing behavior changes. Update docs when a Siril command assumption changes or a risky workflow area is clarified.

## Prefer small changes

Codex should make small, reviewable changes with clear explanations. Large refactors should be avoided unless the user explicitly asks for them.
