# Storage validation results - v3.0.1

Validated on Windows with native Siril 1.4.4 on 2026-09-06. The storage changes are in `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py`; v3.0 was restored byte-for-byte to the pre-change snapshot. README and the locked v2 scripts were not changed.

## Image quality and disk usage

The real-data sample used two nights and two Veil mosaic panels, four full-resolution lights and eight flats per session/panel. Inputs were read-only aliases; separate working directories and private Siril configurations were used. Both registration stages and distortion correction were retained. Comparisons included four flat-master copies, two panel finals and two final-mosaic copies.

| Native comparison | Baseline peak | Managed peak | Result |
| --- | ---: | ---: | --- |
| Background extraction off, uncompressed | 15.76 GiB | 4.82 GiB | All eight decoded arrays and scientific headers identical |
| Background extraction on, GZIP2; test-only dither control in both paths | 19.36 GiB | 3.57 GiB | All eight decoded arrays identical; scientific headers equivalent |

The final controlled run completed from start to finish with v3.0.1: 280 seconds versus 117 seconds for the uncompressed baseline. GZIP2 traded CPU time for space. Managed retained output occupied 1.65 GiB; the pre-run peak estimate was 3.94 GiB, excluding the separate 20 GiB free-space reserve. Peaks were sampled every 100 ms and count unique physical image files, excluding symbolic input aliases; sampling can miss very brief peaks.

The 10347 x 3704 RGB final contains 114,975,864 samples. Its decoded pixels, finite mask, precision, WCS and other scientific headers matched exactly in the controlled comparison. Compressed flat containers omit `EXTEND` and a zero-valued `BZERO`, and Astropy reports different byte order; decoded values and numeric precision are identical. Source/master hashes were unchanged.

Background extraction adds random dithering by default. Two unchanged baseline runs differed (final RMS 4.01e-5); the managed result differed from the first baseline by RMS 6.50e-5. Those numbers alone were not accepted as proof of equivalence. The controlled comparison appended the documented `-nodither` option to **both test paths**, which removed the differences completely. The application retains the original `seqsubsky` settings, including dithering. No production image-processing option was changed to make the test pass.

The earlier background-off development test stopped at a WCS budgeting check, then completed using its retained panels after correcting the check to use two-dimensional celestial WCS. Its report preserves that history. The final controlled comparison above completed without recovery.

## Execution and regression checks

- Python syntax checks and all 43 unit tests passed against v3.0.1. Coverage includes settings migration, shared flat identity, both registrations, 20 combinations of drizzle/background/stack methods, explicit compression commands, Windows paths, selected alias names, FITS/membership checks, changed inputs/settings, corrupt caches, locks, concurrency, early Abort and failure at every consumer stage.
- Native API success, immediate Abort, mid-run Abort and failed registration behaved as expected. Failed consumers retained their prerequisite frames; both cancelled runs resumed successfully.
- Native exported SSF returned exit 0 after successful completion and exit 1 for a failure before Python connected, despite older success receipts remaining. The relative receipt gate also rejected a native conversion/calibration failure during development.
- Generated commands contain no shell `echo` or precision-reduction command. Existing image emitters are unchanged; the managed compiler changes scheduling, storage and cleanup. Existing regression tests now target v3.0.1.
- Long scratch paths exposed a Windows CFITSIO limit. Run/session directory names were shortened, and building now rejects staged paths of 260 or more characters before processing.

## Scope and evidence

This validates the representative sample, not the full 401-light/four-panel project, every camera, or a full native drizzle run. Drizzle and other stack variants have command-generation coverage. Storage guards do not repair Siril's native heap-corruption issue or reserve space against other applications. Existing projects default to Keep intermediates; low-disk mode is opt-in for normal OSC mosaics.

Machine-readable evidence is retained in [validation](validation/storage-native-bge-controlled.json), with separate [background-off](validation/storage-native-bge-off.json), [default-dither repeatability](validation/storage-native-bge-dither.json), [API](validation/storage-native-api.json), [export](validation/storage-native-export.json), and [regression](validation/storage-regression.txt) results. Generated test image trees were removed after preserving these reports.

Reproduce comparisons using `tests/integration_storage.py`; use `tests/integration_storage_api.py` for API recovery checks. See [the testing checklist](testing-checklist.md). The test-only dither option is documented by [Siril's seqsubsky reference](https://siril.readthedocs.io/en/stable/Commands.html#seqsubsky).
