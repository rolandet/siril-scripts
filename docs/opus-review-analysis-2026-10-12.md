# Analysis of the Opus review of Rolandet OSC Stacking

Reviewed on October 12, 2026 against the local active v3.0.1 script and its distribution mirror. Source: [GitLab MR !318 review comment](https://gitlab.com/free-astro/siril-scripts/-/merge_requests/318#note_3987788776), retrieved with GitLab's public GraphQL API. The review's six current-script findings are reproducible in the local source. Both active scripts received the same fixes.

| Suggestion | Assessment | Action |
| --- | --- | --- |
| Use freshly stacked raw calibration masters | Agree; high priority because calibration can silently be omitted | Fixed normal OSC non-mosaic dark and flat-support fallbacks |
| Quote calibration paths | Agree; high priority because ordinary Windows folders contain spaces | Quote entire explicit calibration option tokens in all current emitters |
| Add `shlex` to the exported worker | Agree; blocks recovery when the first distortion solve fails | Added the import and tested retry through the generated worker |
| Install dependencies before importing them | Agree, with standalone and worker distinctions | Bootstrap PyQt6/Astropy in the app when Siril's installer is available; bootstrap Astropy in the worker |
| Apply Canvas Scale to the final product | Agree for the reviewed normal OSC mosaic path | Promote the scaled image for one or multiple panels, including managed runs |
| Use `get_siril_wd()` | Agree; `cmd()` does not return console text | Query the live working directory through the documented API |
| Replace the upstream predecessor with a retirement notice | Agree with recommending migration; retain the old workflow unless replacement is requested | Follow-up adds a dismissible startup notice to local v1.2; its existing application remains available |

## 1. Generated calibration masters

The non-mosaic builder generated `bias_stacked`, `df_stacked`, and `dark_stacked`, but only considered resolved master paths and library variables at their consumption points. Raw darks could therefore satisfy the UI validator without actually being used to calibrate lights. Raw biases and dark-flats similarly failed to support flats. This is the most consequential finding because a successful run could produce incorrectly calibrated data.

The fix preserves resolved explicit master choices, then uses the newly generated dark for lights and dark-flat/bias for flats before automatic library variables. Dark-flats are passed using Siril's current `-dark=` convention; there is no new separate bias subtraction from lights that already use a master dark. Existing selection of a freshly calibrated flat over a supplied flat remains unchanged. Warnings now count the masters actually generated.

## 2. Calibration paths with spaces

Default session names and user calibration folders make this a common failure, especially for narrowband shared masters whose paths are absolute. A shared option formatter now emits an entire quoted token, such as `"-dark=C:/Calibration Masters/dark master.fit"`. Quoting just the value after `=` would not address Siril's argument parser correctly.

The fix covers normal OSC, legacy and managed mosaics, filter-specific narrowband overrides, generated shared masters, and optional broadband calibration. Narrowband warning checks were adjusted to recognize quoted options. Managed plan compilation continues to parse the same quoted tokens and fingerprint their master paths; its separate quoting workaround is no longer necessary.

## 3. Exported-worker import

The application imported `shlex`, but generated workers only copied the runtime definitions plus a smaller import header. Alternate-reference discovery therefore failed in exported workers even though the same logic worked in the application. The header now imports `shlex`. The regression test loads and executes the generated worker in its own namespace, fails the first solve, and verifies the alternate frame and matching `setref` command.

## 4. Dependency bootstrap

Siril's script-author guidance provides `ensure_installed` because end users should not have to manage Siril's Python environment manually. The application now invokes it before importing PyQt6 or Astropy. Keeping `sirilpy` optional preserves the existing standalone application/CLI fallback; that standalone environment still needs its own dependencies when Siril's installer is unavailable.

The exported worker requests Astropy before constructing `StorageRuntime`, where its first FITS import occurs. It does not request PyQt6 because it has no GUI. Installation failures are not swallowed. Installer output is suppressed in that worker bootstrap to preserve the existing protection against filled Python pipes while Siril waits for the worker. Tests verify import order and stop-before-processing behavior on installer failure. A pristine-environment package download was not exercised; the installed environment already has the packages.

## 5. Canvas Scale

The reviewed emitter saved a resized image and then reloaded the original before writing the project final. It also bypassed resizing in its single-panel shortcut. Final promotion now selects the scaled image when the factor differs from 1.0, with the existing final orientation and filename retained. Managed plans track, validate, promote, and clean the scaled scratch product in both panel cases.

Scale 1.0 preserves the previous output path. This fix applies to standard OSC mosaics; narrowband mosaics have separate channel-stitching code and their Canvas Scale behavior remains a known limitation.

## 6. Siril working-directory API

The installed API and official documentation confirm that `cmd()` executes a command and reports errors rather than returning text printed to Siril's console. The live-directory branch now uses `get_siril_wd()` and normalizes the returned path directly. Existing standalone fallbacks remain available if a live directory cannot be obtained. The regression test verifies that a successful live lookup never starts a CLI probe.

## 7. Upstream predecessor retirement

A short notice in the upstream `preprocessing/osc-multi-night-stacking-v1.2.py` menu entry would reduce confusion during the distribution transition. That is a release-packaging request rather than a defect in the new implementation. The initial review retained the local predecessor unchanged.

At the user's follow-up request, local `osc-multi-night-stacking-v1.2.py` now displays a startup notice recommending `Rolandet-OSC-Stacking.py` and describing its added features. Dismissing the notice continues into the existing v1.2 application. This preserves the historical workflow while making the preferred version clear. Nothing was posted to GitLab.

## Validation

- Both current scripts pass Python compilation and the synchronization guard; their bytes match.
- All 55 regression tests pass in Siril's Python environment, including representative normal OSC, NB/broadband, mosaic, and managed plans.
- Eight native synthetic checks pass on Windows Siril 1.4.4: raw bias, raw dark-flat, compressed generated masters, quoted bias/dark, quoted flat/dark, and canvas scales 0.5/1.0/2.0. Calibration arrays are checked numerically; scaled final dimensions and orientation are checked against the saved source image.
- Native evidence: [opus-review-native.json](validation/opus-review-native.json). Reproducible harness: [integration_review_fixes.py](../tests/integration_review_fixes.py).
- Generated commands retain Siril 1.4 syntax and contain no shell-only `echo` commands. Tests cover spaces, session working directories, and final output names.
- These native checks use synthetic data and stop before registration. They do not establish full real-data mosaic/WCS or narrowband image-quality equivalence.

API and command references: [Siril Python API](https://siril.readthedocs.io/en/stable/Python-API.html) and [Siril 1.4 commands](https://siril.readthedocs.io/en/stable/Commands.html).
