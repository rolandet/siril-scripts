# AGENTS.md

## Project

This repository contains Siril scripts and helper tooling for OSC astrophotography preprocessing and stacking, especially multi-night stacking workflows.

The current active development script is:

```text
osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py
```

`osc-multi-night-stacking-v2.1.py` and `osc-multi-night-with-mosiac-stacking-v2.2.py` are locked historical versions. Do not edit them unless the user explicitly asks for changes to those locked versions.

`README.md` is user-maintained. Do not overwrite it or replace its existing content. For repository overview/context, use `docs/repo-overview.md`.

## User workflow context

The scripts are used with Siril 1.4.x on Windows. Output scripts must use valid Siril 1.4 command syntax. Do not assume shell commands such as `echo` are valid inside Siril `.ssf` scripts.

The user often processes OSC data from multiple nights, including lights, darks, flats, and biases/dark-flats. The workflow may include registration, stacking, normalization, optional drizzle, and handling multiple exposure groups.

## Development rules

- Preserve existing behavior unless the task explicitly asks for a change.
- Validate Siril command syntax before changing generated `.ssf` commands.
- Avoid inventing Siril command flags. Check existing project notes first.
- Keep Windows paths in mind.
- Prefer small, reviewable changes.
- Update `CHANGELOG.md` for user-facing behavior changes.
- Update docs when a Siril command assumption changes.

## Testing expectations

Before considering a code change complete:

1. Run Python syntax checks.
2. Test script generation against sample directory structures when available.
3. Review generated Siril commands for Siril 1.4 compatibility.
4. Confirm no unsupported commands such as `echo` are emitted into Siril scripts.
5. Confirm output filenames and working directories are safe for Windows.

For documentation-only changes, do not run the app or execute Siril.

## Important docs

- `docs/repo-overview.md` - current repo orientation and active script context.
- `docs/project-history.md` - history of what was built and why.
- `docs/current-features.md` - feature inventory based on the current script.
- `docs/siril-1.4-command-notes.md` - Siril command syntax notes and gotchas.
- `docs/design-decisions.md` - major design choices.
- `docs/testing-checklist.md` - manual and automated validation steps.
- `docs/known-issues.md` - unresolved bugs, risks, and future work.
