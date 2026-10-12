import builtins
import importlib.util
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


M = load_module("siril_review_tests")


def add_frames(root, kinds):
    (root / "process").mkdir(parents=True, exist_ok=True)
    for kind in kinds:
        folder = root / kind
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "frame.fit").touch()


def calibration_commands(script):
    return [shlex.split(line) for line in script.splitlines() if line.startswith("calibrate ")]


class CalibrationRegressionTests(unittest.TestCase):
    def test_raw_masters_are_used_in_each_session_before_library_variables(self):
        for library in (False, True):
            with self.subTest(library=library), tempfile.TemporaryDirectory(prefix="OSC review ") as tmp:
                root = Path(tmp)
                sessions = [M.Session(name=f"Session {n}") for n in (1, 2)]
                for session in sessions:
                    add_frames(root / session.name, ("lights", "flats", "bias", "dark_flats", "darks"))
                project = M.Project(working_dir=str(root), sessions=sessions,
                                    use_master_library=library, distortion_correction_enabled=False)
                script = M.SirilCommandBuilder(project).build()
                commands = calibration_commands(script)
                flats = [c for c in commands if c[1] == "flat"]
                lights = [c for c in commands if c[1] == "light"]
                self.assertEqual(len(flats), 2)
                self.assertEqual(len(lights), 2)
                for command in flats:
                    self.assertIn("-dark=df_stacked", command)
                    self.assertFalse(any(a.startswith("-bias=") for a in command))
                for command in lights:
                    self.assertIn("-dark=dark_stacked", command)
                    self.assertIn("-cc=dark", command)
                    self.assertIn("-flat=pp_flat_stacked", command)
                self.assertNotIn("WITHOUT bias/dark-flat", script)
                self.assertNotIn("no dark for lights", script)
                # Each relative master is consumed from its own session's process folder.
                cwd = root
                consumed = []
                for line in script.splitlines():
                    args = shlex.split(line) if line and not line.startswith("#") else []
                    if args and args[0] == "cd":
                        cwd = (cwd / args[1]).resolve()
                    if args[:1] == ["calibrate"]:
                        consumed.append(cwd)
                expected = [(root / s.name / "process").resolve() for s in sessions]
                self.assertEqual(consumed, [expected[0], expected[0], expected[1], expected[1]])
                self.assertFalse(any(line.lstrip().startswith("echo ") for line in script.splitlines()))

    def test_raw_bias_is_the_flat_fallback_when_no_dark_flat_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = M.Session(name="Session 1")
            add_frames(root / session.name, ("lights", "flats", "bias"))
            script = M.SirilCommandBuilder(M.Project(working_dir=str(root), sessions=[session])).build()
            flat = next(c for c in calibration_commands(script) if c[1] == "flat")
            self.assertIn("-bias=bias_stacked", flat)
            self.assertNotIn("WITHOUT bias/dark-flat", script)

    def test_explicit_master_overrides_keep_priority_and_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="OSC review ") as tmp:
            root = Path(tmp)
            masters = root / "Calibration Masters"
            masters.mkdir()
            session = M.Session(name="Session 1", master_dark=str(masters / "dark master.fit"),
                                master_bias=str(masters / "bias master.fit"),
                                master_flat=str(masters / "flat master.fit"))
            add_frames(root / session.name, ("lights", "flats", "bias", "dark_flats", "darks"))
            script = M.SirilCommandBuilder(M.Project(working_dir=str(root), sessions=[session])).build()
            flat, light = calibration_commands(script)
            self.assertIn("-bias=" + Path(session.master_bias).as_posix(), flat)
            self.assertIn("-dark=" + Path(session.master_dark).as_posix(), light)
            self.assertNotIn("-dark=dark_stacked", light)
            self.assertNotIn("-dark=df_stacked", flat)
            # Preserve the existing preference for a freshly calibrated flat.
            self.assertIn("-flat=pp_flat_stacked", light)
            (root / session.name / "flats" / "frame.fit").unlink()
            script = M.SirilCommandBuilder(M.Project(working_dir=str(root), sessions=[session])).build()
            light = next(c for c in calibration_commands(script) if c[1] == "light")
            self.assertIn("-flat=" + Path(session.master_flat).as_posix(), light)

    def test_narrowband_and_broadband_share_quoted_generated_masters(self):
        with tempfile.TemporaryDirectory(prefix="NB review ") as tmp:
            root = Path(tmp)
            session = M.Session(name="Session 1")
            session_root = root / session.name
            add_frames(session_root, ("lights", "flats", "bias", "dark_flats", "darks"))
            group_root = session_root / "ha_oiii"
            add_frames(group_root, ("lights", "flats"))
            session.ha_oiii.lights = [str(group_root / "lights" / "frame.fit")]
            project = M.Project(working_dir=str(root), sessions=[session], nb_extraction_enabled=True,
                                nb_output_palette="HOO", nb_use_osc_broadband=True,
                                distortion_correction_enabled=False)
            script = M.SirilCommandBuilder(project).build()
            commands = calibration_commands(script)
            self.assertEqual(len(commands), 4)
            for command in commands:
                name = "df_stacked" if command[1] == "flat" else "dark_stacked"
                self.assertIn("-dark=" + (session_root / "process" / name).as_posix(), command)
            self.assertNotIn("no dark for NB lights", script)
            self.assertNotIn("no dark for lights", script)
            self.assertNotIn("no flat for NB lights", script)
            self.assertNotIn("no flat for lights", script)

    def test_filter_and_mosaic_master_overrides_are_whole_option_tokens(self):
        with tempfile.TemporaryDirectory(prefix="Mosaic review ") as tmp:
            root = Path(tmp)
            master = root / "Calibration Masters" / "master image.fit"
            session = M.Session(name="Session 1", master_dark=str(master), master_flat=str(master),
                                master_dark_flat=str(master))
            group = M.NarrowbandFrameSet(master_dark=str(master), master_flat=str(master),
                                        master_bias=str(master))
            project = M.Project(working_dir=str(root), sessions=[session], mosaic_enabled=True,
                                distortion_correction_enabled=False, link_feather_to_overlap=False)
            builder = M.SirilCommandBuilder(project)
            flat = builder._nb_flat_support_arg(project, session, None, group, {})
            self.assertEqual(shlex.split(flat), ["-bias=" + master.as_posix()])
            light = builder._nb_light_calibration_parts(project, session, None, group, {}, False)
            self.assertIn("-dark=" + master.as_posix(), shlex.split(" ".join(light)))
            self.assertIn("-flat=" + master.as_posix(), shlex.split(" ".join(light)))
            for panel_id in ("P1", "P2"):
                panel = M.Panel(panel_id=panel_id)
                panel_root = root / session.name / panel_id
                add_frames(panel_root, ("lights", "flats"))
                panel.lights = [str(panel_root / "lights" / "frame.fit")]
                panel.flats = [str(panel_root / "flats" / "frame.fit")]
                session.panels.append(panel)
            commands = calibration_commands(builder.build())
            self.assertEqual(len(commands), 4)
            self.assertTrue(all("-dark=" + master.as_posix() in c for c in commands))


class MosaicScalingRegressionTests(unittest.TestCase):
    def test_scaled_image_is_promoted_for_one_or_multiple_panels(self):
        for count in (1, 2):
            for scale in (0.5, 1.0, 2.0):
                with self.subTest(panels=count, scale=scale):
                    project = M.Project(name="Scale Test", mosaic_canvas_scale=scale)
                    lines = []
                    M.emit_phase2_mosaic(work=Path("C:/Mosaic Review"),
                        produced=[f"C:/Mosaic Review/Session 1/P{n}/process/panel_final.fit" for n in range(count)],
                        p=project, L=lines, comp_state=[0], set_comp_if_needed=M.set_comp_if_needed,
                        safe_slug=M.safe_slug, feather_px=0, overlap_norm=False)
                    mirror = lines.index("mirrorx -bottomup")
                    source = "mosaic_final.fit" if scale == 1 else "mosaic_final_scaled.fit"
                    self.assertEqual(shlex.split(lines[mirror - 1]), ["load", source])
                    self.assertEqual(shlex.split(lines[mirror + 1]), ["save", "C:/Mosaic Review/Scale_Test_final.fit"])
                    resamples = [line for line in lines if line.startswith("resample ")]
                    self.assertEqual(resamples, [] if scale == 1 else [f"resample {scale:g}"])


class SirilApiRegressionTests(unittest.TestCase):
    def test_live_working_directory_is_used_without_a_cli_probe(self):
        with tempfile.TemporaryDirectory(prefix="Siril working dir ") as tmp:
            iface = Mock()
            iface.get_siril_wd.return_value = tmp
            widget = SimpleNamespace(siril=SimpleNamespace(iface=iface), project=M.Project())
            with patch.object(M, "find_siril_cli") as find_cli:
                self.assertEqual(M.ProjectWidget._detect_siril_home_dir(widget), os.path.normpath(tmp))
            iface.get_siril_wd.assert_called_once_with()
            iface.cmd.assert_not_called()
            find_cli.assert_not_called()

    def test_app_ensures_dependencies_before_importing_them(self):
        ready = False
        seen = set()

        def ensure(*packages):
            nonlocal ready
            self.assertEqual(packages, ("PyQt6", "astropy"))
            ready = True

        original_import = builtins.__import__

        def require_installed(name, *args, **kwargs):
            if name == "PyQt6" or name.startswith("astropy"):
                self.assertTrue(ready, "Dependency imported before ensure_installed")
                seen.add(name.split(".")[0])
            return original_import(name, *args, **kwargs)

        with patch.dict(sys.modules, {"sirilpy": SimpleNamespace(ensure_installed=ensure)}):
            with patch.object(builtins, "__import__", side_effect=require_installed):
                load_module("siril_bootstrap_tests")
        self.assertEqual(seen, {"PyQt6", "astropy"})

    def test_worker_bootstrap_precedes_runtime_and_failure_stops_execution(self):
        for fails in (False, True):
            with self.subTest(install_failure=fails):
                events = []

                def ensure(*packages):
                    self.assertEqual(packages, ("astropy",))
                    events.append("install")
                    if fails:
                        raise RuntimeError("Cannot install astropy")

                def make_runtime(*args, **kwargs):
                    events.append("runtime")
                    return Mock()

                iface = Mock()
                sirilpy = SimpleNamespace(ensure_installed=ensure, SirilInterface=Mock(return_value=iface))
                with patch.dict(sys.modules, {"sirilpy": sirilpy}), patch.object(sys, "argv", ["worker.py", "manifest.json"]):
                    with patch.object(M, "StorageRuntime", side_effect=make_runtime):
                        if fails:
                            with self.assertRaisesRegex(RuntimeError, "Cannot install astropy"):
                                M.storage_runtime_entry()
                            self.assertEqual(events, ["install"])
                            iface.connect.assert_not_called()
                        else:
                            M.storage_runtime_entry()
                            self.assertEqual(events, ["install", "runtime"])
                            iface.connect.assert_called_once_with()
                            iface.disconnect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
