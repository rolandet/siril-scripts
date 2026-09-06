import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py"
)
SPEC = importlib.util.spec_from_file_location("siril_v3_distortion_tests", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _add_light(root: Path) -> None:
    (root / "lights").mkdir(parents=True)
    (root / "process").mkdir(parents=True)
    (root / "lights" / "light.fit").touch()


class DistortionSettingTests(unittest.TestCase):
    def test_new_project_defaults_follow_workflow(self):
        single = MODULE.Project(sessions=[MODULE.Session(name="Session 1")])
        multi = MODULE.Project(
            sessions=[MODULE.Session(name="Session 1"), MODULE.Session(name="Session 2")]
        )
        mosaic = MODULE.Project(
            sessions=[MODULE.Session(name="Session 1")],
            mosaic_enabled=True,
        )

        self.assertFalse(MODULE.distortion_correction_is_enabled(single))
        self.assertTrue(MODULE.distortion_correction_is_enabled(multi))
        self.assertTrue(MODULE.distortion_correction_is_enabled(mosaic))

    def test_explicit_choice_overrides_recommended_default(self):
        project = MODULE.Project(
            sessions=[MODULE.Session(name="Session 1"), MODULE.Session(name="Session 2")],
            distortion_correction_enabled=False,
        )
        self.assertFalse(MODULE.distortion_correction_is_enabled(project))

    def test_legacy_projects_preserve_previous_behavior(self):
        legacy_normal = MODULE.Project.from_dict({
            "sessions": [{"name": "Session 1"}, {"name": "Session 2"}],
            "mosaic_enabled": False,
        })
        legacy_mosaic = MODULE.Project.from_dict({
            "sessions": [{"name": "Session 1"}],
            "mosaic_enabled": True,
        })

        self.assertFalse(MODULE.distortion_correction_is_enabled(legacy_normal))
        self.assertTrue(MODULE.distortion_correction_is_enabled(legacy_mosaic))

    def test_serialization_writes_resolved_default(self):
        project = MODULE.Project(
            sessions=[MODULE.Session(name="Session 1"), MODULE.Session(name="Session 2")]
        )
        self.assertTrue(project.to_dict()["distortion_correction_enabled"])


class NormalOscDistortionCommandTests(unittest.TestCase):
    def _build(self, root: Path, session_count: int, enabled, pack_mode="off") -> str:
        sessions = []
        for index in range(1, session_count + 1):
            session = MODULE.Session(name=f"Session {index}")
            sessions.append(session)
            _add_light(root / session.name)
        project = MODULE.Project(
            name="Distortion Test",
            working_dir=str(root),
            sessions=sessions,
            use_master_library=True,
            two_pass=True,
            distortion_correction_enabled=enabled,
            pack_sequences_mode=pack_mode,
        )
        return MODULE.SirilCommandBuilder(project).build()

    def test_single_session_enabled_plate_solves_registration_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), 1, True)

        self.assertIn("load pp_light_00001", script)
        self.assertIn("platesolve -force -disto=platesolve_data.wcs", script)
        self.assertIn(
            "register pp_light -layer=0 -2pass -disto=file platesolve_data.wcs",
            script,
        )

    def test_single_session_disabled_preserves_plain_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), 1, False)

        self.assertNotIn("platesolve -force -disto=platesolve_data.wcs", script)
        self.assertNotIn("-disto=file", script)
        self.assertIn("register pp_light -layer=0 -2pass", script)

    def test_multi_session_solves_after_merge_and_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), 2, True)

        merge_at = script.index("merge ")
        load_at = script.index("load all_sessions_00001")
        solve_at = script.index("platesolve -force -disto=platesolve_data.wcs")
        register_at = script.index(
            "register all_sessions -layer=0 -2pass -disto=file platesolve_data.wcs"
        )
        self.assertLess(merge_at, load_at)
        self.assertLess(load_at, solve_at)
        self.assertLess(solve_at, register_at)

    def test_distortion_guard_disables_packed_light_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), 1, True, pack_mode="fitseq")

        self.assertIn("Pack sequences disabled because distortion plate-solving", script)
        self.assertIn("convert light -out=../process", script)
        self.assertNotIn("convert light -fitseq", script)


class MosaicDistortionCommandTests(unittest.TestCase):
    def _build(self, root: Path, enabled) -> str:
        panels = [MODULE.Panel(panel_id="P1"), MODULE.Panel(panel_id="P2")]
        session = MODULE.Session(name="Session 1", panels=panels)
        for panel in panels:
            _add_light(root / session.name / panel.panel_id)
        project = MODULE.Project(
            name="Mosaic Distortion Test",
            working_dir=str(root),
            sessions=[session],
            mosaic_enabled=True,
            distortion_correction_enabled=enabled,
            link_feather_to_overlap=False,
            panel_background_extraction=False,
            use_master_library=True,
        )
        return MODULE.SirilCommandBuilder(project).build()

    def test_mosaic_enabled_uses_distortion_model_for_panel_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), True)

        self.assertIn("load pp_light_00001", script)
        self.assertIn("platesolve -force -disto=platesolve_data.wcs", script)
        self.assertIn(
            "register pp_light -disto=file platesolve_data.wcs -2pass",
            script,
        )

    def test_mosaic_disabled_skips_undistortion_but_keeps_wcs_stitching(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._build(Path(tmp), False)

        self.assertNotIn("platesolve -force -disto=platesolve_data.wcs", script)
        self.assertNotIn("-disto=file", script)
        self.assertIn("register pp_light -2pass", script)
        self.assertIn("seqplatesolve mosaic -force -nocache", script)


class NarrowbandDistortionCommandTests(unittest.TestCase):
    def test_non_mosaic_channels_honor_distortion_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group_root = root / "Session 1" / "ha_oiii"
            _add_light(group_root)
            session = MODULE.Session(
                name="Session 1",
                ha_oiii=MODULE.NarrowbandFrameSet(
                    lights=[str(group_root / "lights" / "light.fit")]
                ),
            )
            project = MODULE.Project(
                name="NB Distortion Test",
                working_dir=str(root),
                sessions=[session],
                nb_extraction_enabled=True,
                distortion_correction_enabled=True,
                use_master_library=True,
                two_pass=True,
            )
            script = MODULE.SirilCommandBuilder(project).build()

        self.assertIn("load Ha_pp_light_00001", script)
        self.assertIn(
            "register Ha_pp_light -layer=0 -2pass -disto=file platesolve_data.wcs",
            script,
        )
        self.assertIn(
            "register OIII_pp_light -layer=0 -2pass -disto=file platesolve_data.wcs",
            script,
        )


if __name__ == "__main__":
    unittest.main()
