# Testing Checklist

Use this checklist before considering code changes complete. For documentation-only changes, do not run the app and do not execute Siril.

## Documentation-only changes

- [ ] Do not run the PyQt app.
- [ ] Do not execute Siril or `siril-cli`.
- [ ] Confirm Python code was not changed.
- [ ] Review `git diff`.
- [ ] Confirm `README.md` was not overwritten or replaced.

## Python checks for code changes

- [ ] Run Python syntax checks, for example `python -m py_compile osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py`.
- [ ] Run `python -B -m unittest discover -s tests -p "test_*.py" -v`.
- [ ] Confirm no hard-coded local-only paths were introduced.
- [ ] Confirm Windows path handling still works.
- [ ] Confirm destructive file operations are still scoped to intended working directories.

## Siril script generation checks

- [ ] Generate `.ssf` output from representative non-mosaic project structures.
- [ ] Generate `.ssf` output from representative mosaic project structures when mosaic behavior is touched.
- [ ] Generate enabled and disabled distortion-correction variants for single-session, multi-session, and mosaic projects when registration behavior is touched.
- [ ] Inspect generated Siril commands manually.
- [ ] Confirm generated scripts contain no shell-only commands such as `echo`.
- [ ] Confirm generated script comments use Siril-compatible comment syntax.
- [ ] Confirm generated filenames are valid on Windows.
- [ ] Confirm working-directory changes are correct.
- [ ] Confirm final output is still saved as `<project_slug>_final.fit` unless intentionally changed.
- [ ] Confirm final output still runs `mirrorx -bottomup` before save unless intentionally changed.

## Siril command assumptions to check

- [ ] `requires 1.4.0`.
- [ ] `setfindstar reset` and `setfindstar`.
- [ ] `setcompress`.
- [ ] `setext fit`.
- [ ] `convert` with `-out`, `-fitseq`, and `-ser`.
- [ ] `calibrate` flags: `-dark=`, `-bias=`, `-flat=`, `-cfa`, `-cc=dark`, `-equalize_cfa`, `-debayer`.
- [ ] Siril Master Library variables: `$defbias`, `$defdark`, `$defflat`.
- [ ] `register` flags: `-layer=0`, `-2pass`, `-drizzle`, `-scale`, `-pixfrac`, `-kernel`, `-disto=file`.
- [ ] `seqapplyreg`, including drizzle args and `-framing=max`.
- [ ] `seqsubsky pp_light 1` when background extraction is enabled.
- [ ] `merge`.
- [ ] `stack` methods and flags: `rej`, `sigma`, `generalized 0.3 0.05`, `mean none`, `med`, `-norm=addscale`, `-nonorm`, `-output_norm`, `-rgb_equal`, `-32b`, `-maximize`, `-feather`, `-overlap_norm`, `-out=`.
- [ ] Mosaic commands: `seqsubsky`, `parse`, `platesolve -force -disto=...`, `seqplatesolve -force -nocache`, `resample`.
- [ ] `load`, `save`, and `mirrorx -bottomup`.

## Workflow checks

- [ ] Calibration frames are discovered and prioritized correctly.
- [ ] Light frames are grouped correctly.
- [ ] Multi-night behavior is preserved.
- [ ] Non-mosaic registration behavior is unchanged unless intentionally modified.
- [ ] Mosaic registration behavior is unchanged unless intentionally modified.
- [ ] New single-session projects default distortion correction off; new multi-session and mosaic projects default it on; explicit and legacy saved values are preserved.
- [ ] Enabled distortion correction emits `load`, `parse`, `platesolve -force -disto=platesolve_data.wcs`, and matching `register ... -disto=file platesolve_data.wcs` commands in that order.
- [ ] Disabled distortion correction omits `platesolve -disto` and `register -disto=file` while retaining ordinary registration and mosaic Phase 2 WCS stitching.
- [ ] Sequence packing is forced off for Mosaic Mode and while distortion correction is enabled.
- [ ] Mosaic mode still disables pack sequences.
- [ ] New mosaic projects default to automatic feathering from overlap percentage; existing saved manual/automatic choices are preserved.
- [ ] Automatic feathering updates immediately when overlap or representative light frames change, without requiring preparation, and the pixel field is read-only while automatic mode is enabled.
- [ ] Automatic feathering uses half of the overlap band on the frame's short edge, returns `0 px` for `0%`, and preserves the `20-300 px` clamp for non-zero overlap.
- [ ] Script generation recalculates automatic feathering and stops with a clear warning if no readable FITS light geometry is available.
- [ ] Automatic feathering reads standard FITS and FPACK tile-compressed FITS geometry, and falls through to later lights when an earlier header is unreadable.
- [ ] Mixed mosaic frame sizes are reported and use the smallest short edge conservatively.
- [ ] Drizzle per panel still forces two-pass behavior.
- [ ] Phase 2 mosaic drizzle remains skipped unless the workflow changes from RGB panel finals to mono/CFA sequences.
- [ ] Non-mosaic background extraction, when enabled, runs after calibration and before registration.
- [ ] Background-extracted non-mosaic workflows use `bkg_pp_light` downstream.
- [ ] Compression behavior is intentional, including `.fit` versus `.fit.fz` loads.
- [ ] Normalization behavior is intentional.
- [ ] Stacking output names are predictable.

## Manual review questions

- [ ] Did this change alter image-processing behavior?
- [ ] Did this change alter file naming?
- [ ] Did this change alter output locations?
- [ ] Did this change add a new Siril command assumption?
- [ ] Does `docs/siril-1.4-command-notes.md` need to be updated?
- [ ] Does `docs/current-features.md` need to be updated?
- [ ] Does `CHANGELOG.md` need to be updated?

## Managed storage validation

Run `python -B -m unittest discover -s tests -p "test_*.py" -v` using an environment with PyQt6, NumPy and Astropy (the Siril Python environment is suitable). Storage tests cover migration, scheduling, command options, flat-key identity, corrupted caches, changed inputs/settings, partial FITS, membership checks, unowned/replaced paths, symlinks, locked files, concurrent-run rejection, cancellation, recovery and UI state.

For opt-in native comparisons use `tests/integration_storage.py --project <project.json> --output <new-test-directory> --siril <siril-cli.exe> --config <config.1.4.ini>`. It uses two nights, two panels, four lights and eight flats per unit by default, writes separate baseline/managed outputs and private config copies, and records image differences and sampled physical storage. `--background on --compression gzip2 --repeat-baseline` measures background-extraction repeatability as well. `--baseline-script` can point to an unchanged source snapshot. The output directory must be new. These are real processing runs, not part of ordinary unit tests.

Verify decoded pixel arrays, nonfinite masks, FITS precision, scientific headers, selection/reference records and transforms. Treat compression container metadata and byte order separately from scientific differences. Do not change tolerances merely to pass a storage optimization. See `storage-validation-results.md` for recorded native results and limits.

Also run exported SSF failure/receipt tests and the `_StorageThread` API path against isolated data. Confirm successful completion, Stop/Abort, retained prerequisites and recovery. Repeatedly updating `state.json` must remain atomic under temporary Windows sharing violations. No cleanup may occur on a failed checkpoint.


`--controlled-no-dither` is a test-only comparison switch: with `--background on`, it appends `-nodither` to both baseline and managed `seqsubsky` commands. It is never enabled by the application. Use this to investigate random background dithering, not to change production image settings.

For the native API/Abort/recovery check, launch `pyscript "<repo>/tests/integration_storage_api.py" "<small-project.json>" "<new-output-directory>"` inside Siril. Read the resulting `report.json` and require `passed: true`, because Siril 1.4.4 ignores Python exit status. Supply absolute paths when launching CLI scripts; Siril starts in its configured working directory.
