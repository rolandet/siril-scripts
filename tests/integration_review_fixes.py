"""Opt-in Siril 1.4.4 checks with synthetic FITS and private config copies.

Run with Siril's Python environment:
  python -B tests/integration_review_fixes.py --config <config.1.4.ini> --report <report.json>
No user image data is read. The calibration check stops before registration.
"""
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from astropy.io import fits


def run_siril(siril, original_config, work, script):
    ssf = work / "check.ssf"
    ssf.write_text(script + "\nclose\n", encoding="utf-8")
    config = work / "siril.ini"
    shutil.copyfile(original_config, config)
    log = work / "siril.log"
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run([str(siril), "-i", str(config), "-s", str(ssf)],
                                cwd=work, stdout=stream, stderr=subprocess.STDOUT, timeout=30)
    if result.returncode:
        raise RuntimeError(f"Siril exit {result.returncode}: {log.read_text(encoding='utf-8', errors='replace')[-5000:]}")


def pixels(path):
    with fits.open(path) as hdus:
        return next(h.data for h in hdus if h.data is not None).astype(float)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--siril", type=Path, default=Path("C:/Program Files/Siril/bin/siril-cli.exe"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.siril = args.siril.resolve(strict=True)
    args.config = args.config.resolve(strict=True)
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("review_native", repo / "Rolandet-OSC-Stacking.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report = {"siril": "1.4.4", "calibration": [], "canvas_scaling": []}
    root = Path(tempfile.mkdtemp(prefix="OSC review native ")).resolve()
    # Check the absolute cleanup target before any recursive removal.
    assert root.parent == Path(tempfile.gettempdir()).resolve()
    try:
        for case, dark_flat, overrides, raw_flats, compression in [
            ("raw bias", False, False, True, False),
            ("raw dark flat", True, False, True, False),
            ("compressed raw masters", True, False, True, True),
            ("quoted bias and dark", False, True, True, False),
            ("quoted flat and dark", False, True, False, False),
        ]:
            work = root / case
            work.mkdir()
            session = module.Session(name="Session 1")
            proc = work / session.name / "process"
            proc.mkdir(parents=True)
            header = fits.Header({"BAYERPAT": "RGGB", "EXPTIME": 60.0})
            gradient = np.tile(np.arange(48, dtype=np.uint16), (32, 1))
            kinds = {"lights": 2000, "darks": 300, "bias": 100}
            if raw_flats:
                kinds["flats"] = 1300
            if dark_flat:
                kinds["dark_flats"] = 200
            for kind, value in kinds.items():
                folder = proc.parent / kind
                folder.mkdir()
                frame = np.full((32, 48), value, dtype=np.uint16)
                if kind == "flats":
                    frame += gradient
                for index in range(1, 4):
                    fits.writeto(folder / f"frame_{index:05d}.fit", frame, header)
            if overrides:
                masters = work / "Calibration Masters"
                masters.mkdir()
                for kind, value in [("dark", 300), ("bias", 100), ("flat", 1200)]:
                    path = masters / f"{kind} master.fit"
                    fits.writeto(path, np.full((32, 48), value, dtype=np.uint16), header)
                    setattr(session, "master_" + kind, str(path))
            # Drizzle's production CFA calibration path avoids demosaicing here,
            # letting us compare subtraction and flat division pixel by pixel.
            project = module.Project(name="Native Review", working_dir=str(work), sessions=[session],
                                     stack_method="Median", drizzle_enabled=True,
                                     distortion_correction_enabled=False, compress_intermediates=compression)
            script = module.SirilCommandBuilder(project).build().split(
                "# ---------------- Global Registration & Stacking ----------------")[0]
            run_siril(args.siril, args.config, work, script)
            ext = ".fit.fz" if compression else ".fit"
            data = pixels(proc / ("pp_light_00001" + ext))
            expected = np.full((32, 48), 1700.0)
            if raw_flats:
                flat_expected = (1100.0 if dark_flat else 1200.0) + gradient
                actual_flat = pixels(proc / ("pp_flat_00001" + ext))
                np.testing.assert_allclose(actual_flat, flat_expected / 65535, rtol=0, atol=2e-6)
                expected *= np.median(flat_expected) / flat_expected
            np.testing.assert_allclose(data, expected / 65535, rtol=0, atol=2e-6)
            report["calibration"].append({"case": case, "passed": True, "shape": list(data.shape)})
            print("PASS:", case, flush=True)

        for scale in (0.5, 1.0, 2.0):
            work = root / f"scale {scale}"
            work.mkdir()
            panel = work / "panel final.fit"
            image = np.linspace(0.1, 0.8, 3 * 32 * 48, dtype=np.float32).reshape(3, 32, 48)
            fits.writeto(panel, image)
            project = module.Project(name="Scale Check", mosaic_canvas_scale=scale)
            lines = ["requires 1.4.0"]
            module.emit_phase2_mosaic(work=work, produced=[str(panel)], p=project, L=lines, comp_state=[0],
                set_comp_if_needed=module.set_comp_if_needed, safe_slug=module.safe_slug,
                feather_px=0, overlap_norm=False)
            run_siril(args.siril, args.config, work, "\n".join(lines))
            data = pixels(work / "Scale_Check_final.fit")
            expected_shape = (3, round(32 * scale), round(48 * scale))
            assert data.shape == expected_shape, (scale, data.shape, expected_shape)
            source = pixels(work / ("mosaic_final.fit" if scale == 1 else "mosaic_final_scaled.fit"))
            np.testing.assert_array_equal(data, source[:, ::-1, :])
            report["canvas_scaling"].append({"scale": scale, "passed": True, "shape": list(data.shape)})
            print("PASS: scale", scale, data.shape, flush=True)
        report["passed"] = True
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        try:
            shutil.rmtree(root)
        except OSError:
            print(f"Synthetic test directory retained because Windows locked a file: {root}")


if __name__ == "__main__":
    main()
