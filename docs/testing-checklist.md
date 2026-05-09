# Testing Checklist

Use this checklist before considering code changes complete. For documentation-only changes, do not run the app and do not execute Siril.

## Documentation-only changes

- [ ] Do not run the PyQt app.
- [ ] Do not execute Siril or `siril-cli`.
- [ ] Confirm Python code was not changed.
- [ ] Review `git diff`.
- [ ] Confirm `README.md` was not overwritten or replaced.

## Python checks for code changes

- [ ] Run Python syntax checks, for example `python -m py_compile osc-multi-night-stacking-v2.1.py`.
- [ ] Run available unit tests, if any.
- [ ] Confirm no hard-coded local-only paths were introduced.
- [ ] Confirm Windows path handling still works.
- [ ] Confirm destructive file operations are still scoped to intended working directories.

## Siril script generation checks

- [ ] Generate `.ssf` output from representative non-mosaic project structures.
- [ ] Generate `.ssf` output from representative mosaic project structures when mosaic behavior is touched.
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
- [ ] `merge`.
- [ ] `stack` methods and flags: `rej`, `sigma`, `mean none`, `med`, `-norm=addscale`, `-nonorm`, `-output_norm`, `-rgb_equal`, `-32b`, `-maximize`, `-feather`, `-overlap_norm`, `-out=`.
- [ ] Mosaic commands: `seqsubsky`, `parse`, `platesolve -force -disto=...`, `seqplatesolve -force -nocache`, `resample`.
- [ ] `load`, `save`, and `mirrorx -bottomup`.

## Workflow checks

- [ ] Calibration frames are discovered and prioritized correctly.
- [ ] Light frames are grouped correctly.
- [ ] Multi-night behavior is preserved.
- [ ] Non-mosaic registration behavior is unchanged unless intentionally modified.
- [ ] Mosaic registration behavior is unchanged unless intentionally modified.
- [ ] Mosaic mode still disables pack sequences.
- [ ] Drizzle per panel still forces two-pass behavior.
- [ ] Phase 2 mosaic drizzle remains skipped unless the workflow changes from RGB panel finals to mono/CFA sequences.
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
