# Siril 1.4 Command Notes

## General rule

Siril `.ssf` scripts are not shell scripts. Do not include shell-only commands.

Unsupported inside Siril scripts:

```text
echo
```

Before adding or changing generated Siril commands, verify the command exists in Siril 1.4.x and that all flags are supported.

## Current generated command families

`osc-multi-night-stacking-v2.1.py` currently emits or may emit these important commands:

- Script setup: `requires 1.4.0`, `cd`, `setfindstar reset`, `setfindstar`, `setcompress`, `setext fit`.
- Conversion: `convert <sequence> -out=...`, optionally with `-fitseq` or `-ser` in non-mosaic mode.
- Calibration: `calibrate flat`, `calibrate light`, `-dark=`, `-bias=`, `-flat=`, `$defbias`, `$defdark`, `$defflat`, `-cfa`, `-cc=dark`, `-equalize_cfa`, `-debayer`.
- Registration: `register`, `-layer=0`, `-2pass`, `-disto=file`, drizzle args.
- Sequence application: `seqapplyreg`, drizzle args, `-framing=max`.
- Stacking: `stack`, `rej`, `rej sigma`, `mean none`, `med`, `-norm=addscale`, `-nonorm`, `-output_norm`, `-rgb_equal`, `-32b`, `-maximize`, `-feather`, `-overlap_norm`, `-out=`.
- Sequence and image operations: `merge`, `load`, `save`, `mirrorx -bottomup`, `resample`, `split ... -lab`.
- Background and mosaic WCS commands: `seqsubsky pp_light 1`, `parse $RA:ra$_$DEC:dec$`, `platesolve -force -disto=platesolve_data.wcs`, `seqplatesolve mosaic -force -nocache`.
- v3.0 narrowband operations: `seqextract_HaOIII pp_light -resample=ha`, `setref <sequence> <image_number>`, `rgbcomp red green blue -out=<name>`, and `rgbcomp -lum=image rgb_image -out=<name>`.

## Known historical errors

### `echo`

Error seen:

```text
Unknown command: 'echo' or not implemented yet
Error in line ... ('echo'): unknown command name.
```

Resolution: do not emit `echo` commands into Siril scripts.

### Unsupported parameters

Previously encountered examples:

```text
Unknown parameter -noout
Unknown parameter -framing=max
```

Resolution: do not rely on guessed command parameters. Validate against Siril 1.4 command syntax before adding or changing flags. The current script intentionally emits `seqapplyreg mosaic -framing=max` for mosaic maximize framing; revalidate before changing this behavior.

## Current gotchas

- Mosaic mode intentionally disables pack sequences because Siril 1.4 cannot plate-solve packed FITSEQ/SER sequences in this workflow.
- Drizzle per panel intentionally forces two-pass registration and uses `seqapplyreg` for drizzle output.
- Phase 2 mosaic drizzle is intentionally skipped because panel finals are RGB, not mono/CFA.
- Non-mosaic background extraction uses `seqsubsky pp_light 1` after calibration and before alignment, then registers the resulting `bkg_pp_light` sequence.
- When drizzle is enabled, OSC light calibration skips `-debayer`.
- When drizzle is off, OSC light calibration includes `-debayer`.
- Narrowband extraction requires calibrated CFA input, so v3.0 narrowband scripts do not emit `-debayer` before `seqextract_HaOIII`.
- v3.0 narrowband scripts intentionally ignore drizzle settings and do not emit drizzle commands in the extraction path.
- Siril's `seqextract_HaOIII` creates `Ha_` and `OIII_` output sequences. The v3.0 SII/OIII workflow uses the same command and treats the red-channel `Ha_` output as SII.
- v3.0 narrowband calibration can use filter-specific master overrides and raw bias, dark, flat, and dark-flat frames from the Ha/OIII or SII/OIII tabs; shared session/panel master overrides and Master Library variables remain fallbacks.
- `setref` takes the sequence name and a one-based image number, not a filename.
- Siril 1.4.3 documents `rgbcomp -lum=image { rgb_image | red green blue } [-out=result_filename]`; v3.0 uses this form for optional OSC-broadband luminance composition.
- Siril 1.4.3 documents `split file1 file2 file3 [-hsl | -hsv | -lab]`; v3.0 uses `split ... -lab` to derive a luminance image from the aligned broadband RGB stack for LRGB output.
- Flat calibration does not use CFA/equalize flags.
- Raw flat calibration prefers dark-flat via `-dark=`, then bias via `-bias=`, then `$defbias` when the Master Library is enabled.
- Final output is intentionally mirrored with `mirrorx -bottomup` before save.
- Compression changes can alter whether generated scripts need to load `.fit` or `.fit.fz`.

## Practical guidance for future changes

When modifying generated Siril scripts:

1. Identify the exact Siril version being targeted.
2. Confirm each command exists.
3. Confirm each command flag exists.
4. Prefer existing known-good command patterns from the current script.
5. Generate `.ssf` output for a representative project.
6. Inspect the generated `.ssf` manually.
7. Add a note here when a new command assumption is validated.

## Open validation items

The following areas should be revalidated before major changes:

- Registration command behavior.
- `seqapplyreg` behavior.
- `seqapplyreg -framing=max`.
- Drizzle command behavior.
- Mosaic WCS solve commands.
- Compression toggling.
- Normalization options such as `addscale`.
- Stacking flags and output naming.
