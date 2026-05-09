# Repository Overview for Codex

This repository contains Siril scripts and helper tooling for OSC astrophotography preprocessing and stacking, with emphasis on multi-night workflows on Windows.

## Current main script

The current main application script is:

```text
osc-multi-night-stacking-v2.1.py
```

Older or adjacent script files may exist in the repository for history or comparison. Do not assume they are the active target unless the user explicitly says so.

## README ownership

`README.md` is user-maintained. Do not overwrite it, replace it, or use it as the source of truth for repo orientation unless the user explicitly asks. Use this `docs/` folder for Codex-facing project context.

## Application shape

The current app is a single-file PyQt6 desktop application. It contains:

- Dataclass project models: `Project`, `Session`, and `Panel`.
- UI widgets for sessions, panels, project options, mosaic options, and run controls.
- Working-directory preparation logic.
- Calibration validation logic.
- Siril `.ssf` generation in `SirilCommandBuilder`.
- Optional in-Siril execution through `sirilpy`.
- `siril-cli` fallback execution and log streaming.

## Project focus

The main workflow is OSC multi-night stacking for Siril 1.4.x. The workflow may include:

- Multi-session light-frame organization.
- Per-session and per-panel calibration frames.
- Master calibration overrides and Siril Master Library variables.
- Registration, optional two-pass registration, and stacking.
- Optional drizzle handling.
- Optional pack sequences in non-mosaic mode.
- Experimental mosaic workflows.
- Generated Siril `.ssf` scripts.

## Development with Codex

Before making code changes, Codex should read:

```text
AGENTS.md
docs/repo-overview.md
docs/project-history.md
docs/current-features.md
docs/siril-1.4-command-notes.md
docs/design-decisions.md
docs/testing-checklist.md
docs/known-issues.md
```

Then inspect `osc-multi-night-stacking-v2.1.py`.

## Important cautions

Siril `.ssf` scripts are not shell scripts. Do not emit shell-only commands such as `echo`.

Treat Siril 1.4 command syntax as strict and version-specific. Prefer the existing generated command patterns unless the user asks for a behavior change and the Siril command syntax has been validated.

Keep Windows path handling in mind. The script generally converts generated Siril paths to POSIX-style slashes while preserving Windows drive roots.
