# Known Issues and Future Work

## Known issues and risky existing behavior

- Mosaic canvas scaling currently saves `mosaic_final_scaled.fit`, but final promotion loads `mosaic_final.fit`. Treat this as existing behavior unless the user asks to change it.
- The About dialog text appears to contain `winorized` instead of `winsorized`.
- `MainWindow.__init__` appears to contain duplicated initial window sizing/centering logic.
- The stack-method combo currently has four visible items, while `_stack_method_map` includes five indices. Generated stack commands use `map_stack_method()` and current text, but the stale map is a cleanup candidate.
- Geometry preflight runs during script generation and can move converted light files into `_mismatch_geometry` when `astropy` is available. This is useful but surprising for a "build script" action.
- Session removal and "Remove Data (All Sessions)" delete on-disk working folders. Future changes should preserve prompts and consider path containment checks.

## Historical problem areas

These areas have caused problems or confusion in previous development and should be treated carefully:

- Unsupported Siril commands in generated `.ssf` scripts.
- Unsupported Siril command flags.
- Shell-only commands such as `echo` accidentally emitted into `.ssf`.
- Registration versus `seqapplyreg` behavior.
- Maximize framing behavior.
- Drizzle behavior and file-size implications.
- Compression toggling and `.fit` versus `.fit.fz` output names.
- Windows path handling.
- Generated script comments and logging.
- Siril Master Library variable assumptions.
- Mosaic WCS and plate-solving assumptions.
- Pack sequence interaction with mosaic plate solving.

## Future work ideas

- Add automated tests for generated Siril script content.
- Add sample non-mosaic and mosaic project structures for script-generation tests.
- Add expected `.ssf` snapshots for representative workflows.
- Add command-syntax validation notes for Siril 1.4.x as commands are verified.
- Add path containment checks around destructive working-directory cleanup.
- Consider splitting the single-file app only if the user asks for a refactor.

## Low-disk mode limits

- Low-disk processing currently supports normal OSC mosaics on Siril 1.4.4. Keep intermediates is required for other modes. This does not fix or diagnose a native Siril heap-corruption crash.
- The peak estimate uses uncompressed sizes and an explicit reserve. WCS canvas estimates remain provisional until solving; large gaps, drizzle, copied aliases, locked files and other applications' disk use can increase requirements. Guards check free space before each allocating stage; they cannot reserve the filesystem against other processes.
- Recovery retains completed panels, not every intermediate stage. Changed sources, masters or relevant Siril settings require a new run. Unsupported library expressions disable reuse and recovery rather than assuming equivalence.
- Deleting temporary sequences removes the ability to immediately re-stack them. Raw inputs can recreate them. Existing unrelated working folders and older run bundles are never automatically adopted or cleared; completed bundles can be explicitly selected for removal through **Remove Data (All Sessions)**.
- Final files and per-panel generated masters are kept at the legacy user-facing locations. Shared flat caches and diagnostic files remain in each run bundle; completed scratch duplicates are released after promotion.
