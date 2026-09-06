# Storage optimization plan for OSC mosaics

Status: implementation added; native validation results are recorded in `storage-validation-results.md`.
Date: 2026-09-06.
Target: `osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py` (v3.0 retained unchanged), Siril 1.4.x on Windows.

## Objective and correction to the initial estimate

Reduce peak temporary disk usage while retaining the current image-processing operations and verifying equivalent scientific outputs. Keep existing projects and other processing modes compatible.

The initial estimate of approximately 104 GiB of avoidable output included approximately 90 GiB from removing one registration stage. That is an algorithm change, not a proven storage-only optimization. The initial registration applies each session/panel's distortion model; subsequent registration aligns nights. Changing those stages can change reference selection, resampling, rejected frames, and drizzle behavior. Both stages remain in this plan.

The primary savings come from shortening file lifetimes and processing independent panels sequentially. These reduce peak storage even though most intermediate images are still generated. Exact flat reuse additionally reduces total computation and bytes written.

## Evidence and current limits

The September 5 run produced 381.0716 GiB of regular files in its process directories, excluding symbolic links. Approximately 360.88 GiB was four RGB light-sequence generations. Each processed light was 5496 x 3672 x 3, 32-bit float, 242,182,080 bytes including FITS overhead.

The JSON contains 401 lights across two nights and four panels. Each night's four panels list the same ordered set of 30 flats. Repeating those flat calibrations accounts for approximately 13.5 GiB of additional calibrated-flat data compared with one set per night.

At this planning review, the JSON has compression enabled, the old Session 1/Session 2 working folders are absent, and completed mosaic/postprocessing files are present. Therefore the previous flat masters cannot now be compared pixel for pixel. The plan requires a fresh controlled comparison and does not claim that equality has already been demonstrated.

The previously inspected crash was Siril 1.4.4 heap corruption. Reducing storage pressure is not proof of fixing that native crash.

## Scope and compatibility

1. Introduce a persisted storage policy: **Keep intermediates** and **Low disk usage**. Old projects with no policy retain the existing behavior. Selecting Low disk usage authorizes cleanup of that run's generated temporary files; no per-file prompts are needed.
2. Initially apply the new scheduling/cleanup policy to normal OSC mosaic processing, the path responsible for the measured problem. Preserve non-mosaic and narrowband builders and verify their generated commands are unchanged. Do not silently apply an untested policy to those modes.
3. Preserve both registrations, per-night distortion models, drizzle settings, calibration precedence, input ordering within each sequence, background extraction, rejection/normalization settings, channel handling, framing, orientation, and final output names/locations.
4. Retain raw inputs, supplied calibration masters, reusable generated masters, completed panel finals, final mosaic products, scripts, project JSON, and run logs.
5. Explain the operational tradeoff: Low disk usage removes the ability to immediately re-stack deleted intermediate sequences. Raw data can recreate them. Keep intermediates remains available for inspection and experimentation.
6. Do not include unrelated reference-selection, canvas-scaling, or calibration-warning fixes in the image-equivalence comparison. Review those separately.

## Implementation sequence

### 1. Establish ownership, checkpoints, and a storage estimate

- Create an execution manifest identifying the run, input fingerprints, commands, generated artifacts, dependencies, and completion state. Treat it as data, not executable code.
- Use an isolated, run-owned scratch location within the project's working area. Preserve user-facing output locations. Never adopt arbitrary pre-existing files as disposable run output.
- Track aliases and their real backing files. In particular, `ALL_*` links keep `r_bkg_pp_light_*` alive until cross-night processing no longer requires them.
- Validate canonical parent paths, reject escaping junctions/reparse points, and protect all source/master/output identities even if they are inside the project directory. Unlink generated aliases without traversing or deleting their targets.
- Delete only explicitly recorded artifacts whose last consumer completed successfully. Do not recursively wipe session folders or use broad filename wildcards.
- Require actual command completion, expected sequence membership, readable FITS structure and complete payloads before releasing prerequisites. File existence or a progress-log percentage is insufficient. Account for legitimate registration exclusions rather than expecting every input to survive.
- Record successful checkpoints atomically before cleanup. After a failure or abort, preserve unfinished-stage inputs and completed panel finals. Resume only when inputs, relevant settings, commands, and required checkpoint outputs match; otherwise recompute from retained prerequisites or raw data.
- Estimate peak live files from dimensions, channels, data type, counts, drizzle scale, link/copy behavior, and retention policy. Recheck free space before large stages. Treat a mosaic canvas estimate as provisional until registration/WCS geometry is available.
- Reserve space for Windows, page-file growth, Siril swap, and estimator uncertainty. Use uncompressed sizes as the conservative basis unless measured compression data supports a smaller estimate.

### 2. Add synchronous cleanup without changing processing order

Native testing required an execution adjustment: Siril 1.4.4 waits for `pyscript` but ignores its Python exit status, and verbose child output can block on buffered pipes. A single generated Python controller therefore owns the image commands and checks each synchronous Siril API result before filesystem validation and cleanup. API execution uses that controller directly in a worker thread; CLI/exported SSF uses one `pyscript` invocation, followed by a fresh single-use FITS receipt and final-image display. Python diagnostics are written to files. The application additionally verifies fresh completion state and never automatically falls back to another execution mode after starting a managed run.

The controller's native command execution, failures, receipt gate and recovery are tested with Siril 1.4.4. Earlier Siril releases are not enabled for this policy. Processing commands continue to come from the existing generator; this execution change adds no image operation or altered numerical setting.

| Successful stage | Temporary data eligible for release |
| --- | --- |
| Flat master built and validated | That master's converted/calibrated flat sequences, after all consumers are accounted for |
| Background-corrected sequence complete | Its `pp_light_*` images, when no later operation needs them |
| Per-night registered output complete | Its background-corrected input images and no-longer-needed metadata |
| Cross-night registered output complete | Its merge aliases and per-night registered backing images, once all references are released |
| Panel stack complete | Its remaining registered subframes and scratch metadata |
| Final mosaic complete | Mosaic-only scratch products; retain all panel finals and designated final outputs |

For each boundary, audit actual Siril state and later command dependencies, including loaded sequences and drizzle flat weights. If safe release cannot be established, keep the files until a later boundary. A failed cleanup is logged and the space estimate is updated before continuing.

### 3. Reuse only provably identical flat calculations

- Cache within the run and, initially, within the same session. Match the ordered actual flat inputs, multiplicity, immutable file fingerprints, geometry/CFA metadata, effective bias/dark-flat selections, Master Library resolution/settings, flat-calibration arguments, stacking method/rejection/normalization, and numeric output settings.
- Same count, filename, camera, or filter is not sufficient evidence of equivalence.
- Preserve panel/filter-specific overrides. If effective calibration inputs cannot be resolved confidently, build a separate master.
- Invalidate reuse when any dependency changes. Cross-run cache reuse is deferred to avoid stale-master risks.
- Keep shared masters until their last light-calibration or drizzle consumer completes. Preserve per-panel master access using safe aliases or small master copies where needed for existing paths.
- Require identical master pixel arrays for a deterministic comparison before accepting reuse. This step should remove six redundant 30-frame flat-calibration batches from the reviewed dataset.

### 4. Complete each panel before starting the next

Change only the order of independent panel work:

1. Process all contributing nights for panel R1C1 using the same per-night operations.
2. Perform the existing cross-night merge, registration, and stack for R1C1.
3. Validate and retain R1C1's final; release its disposable sequences.
4. Repeat for R1C2, R2C1, and R2C2 in the original panel order.
5. Execute the existing mosaic assembly on the retained panel finals.

Preserve each panel's night/frame ordering, merge destination/reference conventions, and relevant Siril state at entry to each operation. Reordering may interact with randomized background sampling or hidden global state; it requires the image comparisons below. If an unexplained difference appears, retain the original scheduling while keeping the independently verified cleanup savings.

### 5. Make optional compression explicitly lossless

- Low disk usage must preserve the existing floating-point precision. Permit uncompressed intermediates or explicitly lossless FITS compression; do not enable quantized floating-point Rice compression as a quality-preserving shortcut.
- Validate the exact Siril 1.4 command for GZIP2 with quantization disabled, including `.fit`/`.fit.fz` sequence discovery, master lookup, registration, stacking, and final output promotion.
- Require bit-identical decoded pixel arrays and preservation of scientific header values on compression round trips. Update the UI wording so it accurately distinguishes lossless compression from existing preference-driven compression.
- Migrate existing compression settings without silently reinterpreting saved choices. Keep lossless compression optional because its size reduction and runtime cost vary.
- Attribute no fixed savings to compression in the initial disk budget. Cleanup and scheduling must provide the core reduction on their own.

## Storage expectations for the reviewed dataset

These are planning estimates based on the earlier uncompressed measurements, not measured performance of an implementation.

| Configuration | Estimated peak image-file footprint |
| --- | --- |
| September 5 run before mosaic assembly | 381.07 GiB observed |
| Stage cleanup, original scheduling, both registrations retained | Approximately 115 GiB, plus uncertain overhead |
| Stage cleanup and one-panel-at-a-time scheduling, both registrations retained | Approximately 47 GiB in the simplified model; target roughly 50-60 GiB with working overhead |

The panel estimate includes simultaneous per-night and cross-night registered frames for the largest 101-frame panel. It assumes successful stage cleanup, link-based merges as observed, no drizzle, and the reviewed frame dimensions. Different frame counts, copy fallback, drizzle, large or abnormal mosaic canvases, retained checkpoints, and cleanup failures increase the budget. Free-space requirements must additionally include the operating-system/Siril reserve.

## Acceptance tests and release criteria

1. **Baseline:** use the unchanged current builder and fixed input/settings snapshots. Run a manageable real two-night, multi-panel dataset through the original and proposed paths in separate scratch directories. Do not use the user's only working copies or already-processed final as the sole baseline.
2. **Image equality:** compare decoded pixel arrays, masks/nonfinite pixels, frame inclusion, reference selection, transforms, WCS, dimensions, channel order, exposure/stack counts, and final orientation. Ignore only non-scientific differences such as timestamps and history paths. Check flat masters, panel finals, and the final mosaic.
3. **Nondeterminism:** repeat the baseline where necessary to measure intrinsic variation from background-sample dithering or floating-point parallel reductions. Investigate differences beyond baseline repeatability; visual resemblance alone is not acceptance. Compare stellar FWHM/shape/flux, background level/noise, and mosaic overlap/seams as supporting checks. Do not silently loosen tolerances to pass an optimization.
4. **Features:** generate and review single/multi-night mosaics, missing/asymmetric panel coverage, shared/distinct calibration, library and explicit master overrides, background extraction on/off, distortion on/off, one/two-pass registration, drizzle variants, all stack methods, framing options, and compression combinations. Verify unchanged non-mosaic/NB command output and existing tests.
5. **Recovery and file safety:** inject failure/abort before and after every deletion checkpoint; test stale/partial outputs, changed settings, missing inputs, link chains, junctions, external masters, locked files, insufficient space, and concurrent-run rejection. Confirm preserved source hashes and correct resume/recompute decisions.
6. **Execution:** validate API, CLI, and exported-SSF behavior, progress/logging, Stop/Abort, and final-image display. Capture durable stage/error logs for both API and CLI runs. Do not automatically retry a partially executed managed run through another execution path.
7. **Storage:** measure actual peak physical usage throughout a representative run, including copy fallback and compression, and compare against the estimator. Report peak live storage separately from total bytes written and final retained storage.
8. **Repository:** Python syntax checks, relevant unit/integration tests, supported Siril commands only, safe quoted Windows paths, and updated CHANGELOG/current-features/command-notes/testing-checklist. Preserve README and locked historical scripts.

Enable the new policy only after the applicable gates pass. Cleanup alone cannot preserve arbitrary intermediate reprocessing convenience; the storage-policy choice makes that tradeoff explicit.

## Reference documentation

- [Siril FAQ: deletion and retention of intermediates](https://siril.org/faq/#could-you-add-commands-to-delete-files-or-sequences)
- [Siril 1.4.4 command reference](https://siril.readthedocs.io/en/stable/Commands.html)
- [Siril 1.4.4 Python API](https://siril.readthedocs.io/en/stable/Python-API.html)
- [Siril FITS compression and floating-point quantization](https://siril.readthedocs.io/en/stable/file-formats/FITS.html#compression)
