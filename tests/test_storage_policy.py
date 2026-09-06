import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.io import fits

SCRIPT = Path(__file__).resolve().parents[1] / 'osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py'
SPEC = importlib.util.spec_from_file_location('siril_storage_tests', SCRIPT)
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


def fixture(root, nights=2, panels=2):
    raw = root / 'raw'
    raw.mkdir()
    header = fits.Header({'BAYERPAT': 'RGGB', 'INSTRUME': 'Test', 'GAIN': 100, 'OFFSET': 8})
    lights, flats = [], []
    for kind, target in [('light', lights), ('flat', flats)]:
        for index in range(3):
            path = raw / f'{kind}_{index:03d}.fit'
            fits.writeto(path, np.full((16, 24), 1000 + index, dtype=np.uint16), header)
            target.append(str(path))
    bias = raw / 'bias master.fit'
    fits.writeto(bias, np.full((16, 24), 100, dtype=np.uint16), header)
    project = M.Project(name='Test Mosaic', working_dir=str(root / 'work'), mosaic_enabled=True,
                        storage_policy='min_disk', use_master_library=False, allow_uncalibrated=True,
                        panel_background_extraction=True, distortion_correction_enabled=True,
                        link_feather_to_overlap=False)
    project.sessions = [M.Session(name=f'Night {n}', master_bias=str(bias),
        panels=[M.Panel(panel_id=f'P{i}', lights=list(lights), flats=list(flats)) for i in range(panels)])
        for n in range(nights)]
    builder = M.SirilCommandBuilder(project)
    script = builder.build()
    bundle = builder.storage_bundle
    bundle.write()
    (Path(project.working_dir) / 'run_project.ssf').write_text(script, encoding='utf-8')
    return project, bundle, script


class FakeSiril:
    def __init__(self, runtime, fail=None, cancel=None):
        self.runtime = runtime
        self.fail = fail
        self.cancel = cancel
        self.commands = []
        self.produced = set()

    def get_siril_log(self):
        return ''

    def get_siril_config(self, group, key):
        if key.endswith('_lib'):
            return ''
        return False

    def cmd(self, command):
        self.commands.append(command)
        stage = self.runtime.current
        if not stage or command == 'close':
            return
        if self.fail and self.fail in command:
            raise RuntimeError('Injected Siril failure')
        if self.cancel and self.cancel in command:
            self.runtime.cancel_path.write_text('cancel')
            return
        if command != stage['commands'][-1] or stage['label'] in self.produced:
            return
        self.produced.add(stage['label'])
        for artifact in stage['created']:
            base = Path(artifact['path'])
            base.parent.mkdir(parents=True, exist_ok=True)
            count = artifact.get('count', artifact.get('max_count', 1))
            names = [base] if artifact['kind'] == 'file' else [base.with_name(base.name.rstrip('_') + f'_{i+1:05d}.fit') for i in range(count)]
            for path in names:
                fits.writeto(path, np.ones((3, 16, 24), dtype='float32'), overwrite=True)
            if artifact['kind'] == 'sequence':
                seq = Path(str(base).rstrip('_') + '_.seq')
                seq.write_text('\n'.join(f'I {i+1} 1' for i in range(count)))
            if artifact.get('selected_from'):
                Path(artifact['selected_from'].rstrip('_') + '_.seq').write_text('\n'.join(f'I {i+1} 1' for i in range(count)))


def runtime_for(bundle, **options):
    runtime = M.StorageRuntime(bundle.manifest_path, None, reserve_bytes=0)
    runtime.iface = FakeSiril(runtime, **options)
    runtime.log = lambda message: None
    # WCS budgeting is exercised in the native integration test; fake files have no WCS.
    runtime.mosaic_bytes = lambda prefix: 0
    return runtime


class StoragePolicyTests(unittest.TestCase):
    def test_legacy_migration_does_not_reinterpret_compression(self):
        p = M.Project.from_dict({'compress_intermediates': True})
        self.assertEqual(p.storage_policy, 'keep_all')
        self.assertEqual(p.low_disk_compression, 'off')
        self.assertTrue(p.compress_intermediates)
        p.storage_policy, p.low_disk_compression, p.storage_reserve_gib = 'min_disk', 'gzip2', 30
        q = M.Project.from_dict(p.to_dict())
        self.assertEqual((q.storage_policy, q.low_disk_compression, q.storage_reserve_gib), ('min_disk', 'gzip2', 30))

    def test_unsupported_modes_refuse_managed_policy(self):
        for mosaic, nb in [(False, False), (True, True), (False, True)]:
            with self.subTest(mosaic=mosaic, nb=nb):
                p = M.Project(working_dir='.', storage_policy='min_disk', mosaic_enabled=mosaic, nb_extraction_enabled=nb)
                with self.assertRaisesRegex(ValueError, 'normal OSC mosaics'):
                    M.SirilCommandBuilder(p).build()

    def test_panel_scheduling_both_registrations_and_flat_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, b, script = fixture(Path(tmp))
            order = [s['panel'] for s in b.stages]
            self.assertEqual(order, sorted(order, key=['P0', 'P1', '__mosaic__'].index))
            commands = '\n'.join(c for s in b.stages for c in s['commands'])
            self.assertEqual(commands.count(' -disto=file platesolve_data.wcs -2pass'), 4)
            self.assertEqual(commands.count('register ALL_'), 2)
            self.assertEqual(commands.count('seqsubsky pp_light 1'), 4)
            masters = [s for s in b.stages if s.get('flat_key')]
            self.assertEqual(masters[0]['flat_key'], masters[2]['flat_key'])
            self.assertNotEqual(masters[0]['flat_key'], masters[1]['flat_key'])
            self.assertIn('"-bias=', commands)
            self.assertNotIn('echo ', commands)
            self.assertNotIn('set16bits', commands)
            self.assertNotIn('set32bits', commands)
            self.assertIn('load completed.fit', script)
            self.assertTrue((b.folder / 'failure_gate').is_dir())
            self.assertFalse(b.root.exists())  # Build never prepares or cleans processing data.
            self.assertEqual(M.storage_manifest_for_script(Path(p.working_dir) / 'run_project.ssf', p), b.manifest_path)
            p.two_pass = not p.two_pass
            with self.assertRaisesRegex(ValueError, 'settings'):
                M.storage_manifest_for_script(Path(p.working_dir) / 'run_project.ssf', p)

    def test_feature_commands_and_explicit_lossless_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _, _ = fixture(Path(tmp), panels=1)
            for drizzle in (False, True):
                for background in (False, True):
                    for method in ('mean', 'median', 'Winsorized', 'Sigma', 'GESDT'):
                        q = copy.deepcopy(p)
                        q.drizzle_enabled = drizzle
                        q.drizzle_scaling = 1.5
                        q.panel_background_extraction = background
                        q.stack_method = method
                        q.low_disk_compression = 'gzip2'
                        b = M.SirilCommandBuilder(q)
                        b.build()
                        commands = '\n'.join(c for s in b.storage_bundle.stages for c in s['commands'])
                        self.assertIn('setcompress 1 -type=gzip2 0', commands)
                        self.assertNotIn('\nsetcompress 1\n', commands)
                        self.assertEqual('-drizzle -scale=1.5' in commands, drizzle)
                        self.assertEqual('seqsubsky pp_light 1' in commands, background)
                        cal = [c for s in b.storage_bundle.stages for c in s['commands'] if c.startswith('calibrate light')]
                        self.assertTrue(all(('-debayer' in c) != drizzle for c in cal))

    @unittest.skipUnless(os.name == "nt", "Windows CFITSIO path constraint")
    def test_long_staged_path_is_rejected_before_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = fixture(Path(tmp), panels=1)
            project.working_dir = str(Path(tmp) / ("long_work_folder_" * 14))
            with self.assertRaisesRegex(ValueError, "Windows path limit"):
                M.SirilCommandBuilder(project).build()

    def test_duplicate_names_and_mixed_geometry_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _, _ = fixture(Path(tmp), panels=1)
            p.sessions[0].panels[0].lights *= 2
            with self.assertRaisesRegex(ValueError, 'Duplicate input filenames'):
                M.SirilCommandBuilder(p).build()
            p.sessions[0].panels[0].lights = p.sessions[0].panels[0].lights[:3]
            fits.writeto(p.sessions[0].panels[0].lights[0], np.zeros((5, 5)), overwrite=True)
            with self.assertRaisesRegex(ValueError, 'geometry'):
                M.SirilCommandBuilder(p).build()


class StorageRuntimeTests(unittest.TestCase):
    def test_success_reuses_flats_cleans_sequences_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, b, _ = fixture(Path(tmp))
            before = {name: M.StorageRuntime.fingerprint(name) for name in b.plan['protected']}
            r = runtime_for(b)
            r.run()
            self.assertEqual(r.state['status'], 'complete')
            self.assertEqual(sum(c.startswith('stack pp_flat') for c in r.iface.commands), 2)
            self.assertEqual(len(r.state['panels']), 3)
            self.assertTrue(all(before[n] == r.fingerprint(n) for n in before))
            self.assertTrue(Path(b.plan['final']).exists())
            self.assertGreater(r.state['bytes_deleted'], 0)
            self.assertFalse(any(rec['artifact'].endswith(('pp_light', 'r_mosaic', 'ALL_P0', 'ALL_P1')) for rec in r.state['owned'].values()))
            r2 = runtime_for(b)
            r2.run()
            self.assertEqual(r2.iface.commands[:2], ['requires 1.4.4', 'close'])
            self.assertEqual(len(r2.iface.commands), 3)
            self.assertNotEqual(r.receipt, r2.receipt)
            self.assertEqual(r2.iface.commands[-1], 'cd "' + r2.receipt.parent.as_posix() + '"')

    def test_failed_consumer_retains_inputs_and_restarts_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b, fail='seqsubsky')
            with self.assertRaisesRegex(RuntimeError, 'Injected'):
                r.run()
            self.assertEqual(r.state['status'], 'failed')
            self.assertFalse(r.receipt.exists())
            self.assertEqual(len(list(b.root.rglob('pp_light_*.fit'))), 3)
            resumed = runtime_for(b)
            resumed.run()
            self.assertEqual(resumed.state['status'], 'complete')

    def test_cancel_after_command_cannot_commit_or_clean_its_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b, cancel='seqsubsky')
            with self.assertRaises(InterruptedError):
                r.run()
            self.assertEqual(r.state['status'], 'cancelled')
            self.assertEqual(len(list(b.root.rglob('pp_light_*.fit'))), 3)
            self.assertFalse(r.receipt.exists())

    def test_changed_inputs_and_configuration_block_resume_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b, fail='seqsubsky')
            with self.assertRaises(RuntimeError):
                r.run()
            kept = list(b.root.rglob('pp_light_*.fit'))
            with fits.open(b.plan['protected'][0], mode='update') as hdus:
                hdus[0].header['TESTCHG'] = 1
            with self.assertRaisesRegex(ValueError, 'changed'):
                runtime_for(b).run()
            self.assertTrue(all(p.exists() for p in kept))

    def test_space_failure_leaves_raw_data_and_emits_no_image_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b)
            with patch.object(M.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
                with self.assertRaisesRegex(OSError, 'disk space'):
                    r.run()
            self.assertEqual(r.iface.commands, ['requires 1.4.4', 'close'])
            self.assertFalse(r.receipt.exists())

    def test_truncated_output_and_wrong_membership_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b)
            b.root.mkdir(parents=True)
            broken = b.root / 'bad.fit'
            fits.writeto(broken, np.zeros((100, 100), dtype='float32'))
            with broken.open('r+b') as stream:
                stream.truncate(3000)
            with self.assertRaisesRegex(ValueError, 'Truncated'):
                r.validate_fits(broken)
            r.current = next(s for s in b.stages if s['label'].endswith('night registration'))
            r.iface.cmd(r.current['commands'][-1])
            output = r.current['outputs'][0]
            Path(output['selected_from'] + '_.seq').write_text('I 1 0\nI 2 1\nI 3 1')
            with self.assertRaisesRegex(ValueError, 'membership'):
                r.validate(r.current)

    def test_unowned_or_replaced_file_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b)
            b.root.mkdir(parents=True)
            stray = b.root / 'personal.fit'
            stray.write_bytes(b'keep')
            with self.assertRaisesRegex(ValueError, 'unowned'):
                r.remove([str(stray)])
            a = b.stages[0]['outputs'][0]
            out = Path(a['path']);out.parent.mkdir(parents=True)
            fits.writeto(out,np.zeros((16,24)))
            r.remember(b.stages[0])
            out.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed outside'):
                r.remove([str(out)])
            self.assertTrue(out.exists())
            self.assertTrue(stray.exists())

    def test_directory_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            r = runtime_for(b)
            b.root.mkdir(parents=True)
            target=Path(tmp)/'external';target.mkdir()
            link=b.root/'escape'
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest('Directory symlink privilege unavailable')
            with self.assertRaisesRegex(ValueError, 'reparse'):
                r.safe_path(link/'image.fit')

    def test_project_lock_rejects_concurrent_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, b, _ = fixture(Path(tmp), panels=1)
            first, second = runtime_for(b), runtime_for(b)
            first.lock()
            try:
                with self.assertRaisesRegex(RuntimeError, 'Another managed run'):
                    second.lock()
            finally:
                first.lock_file.close()



class StorageAdditionalSafetyTests(unittest.TestCase):
    def test_corrupt_flat_cache_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, _ = fixture(Path(tmp), panels=1)
            first = runtime_for(bundle, fail="seqsubsky")
            with self.assertRaises(RuntimeError):
                first.run()
            cache = Path(next(iter(next(iter(first.state["flats"].values())))))
            cache.write_bytes(b"corrupt")
            resumed = runtime_for(bundle)
            with self.assertRaisesRegex(ValueError, "cached flat"):
                resumed.run()
            self.assertFalse(any(c.startswith("calibrate ") for c in resumed.iface.commands))

    def test_early_abort_survives_startup_and_resume_clears_old_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, _ = fixture(Path(tmp), panels=1)
            runtime = runtime_for(bundle)
            runtime.cancel_path.write_text(str(M.time.time_ns()), encoding="utf-8")
            with self.assertRaises(InterruptedError):
                runtime.run()
            self.assertEqual(runtime.iface.commands, [])
            self.assertFalse(runtime.receipt.exists())
            resumed = runtime_for(bundle)
            resumed.run()
            self.assertEqual(resumed.state["status"], "complete")

    def test_atomic_checkpoint_retries_transient_lock_and_stops_on_persistent_lock(self):
        with patch.object(M.os, "replace", side_effect=[PermissionError(), None]) as replace, patch.object(M.time, "sleep"):
            M.StorageRuntime.atomic_replace("source", "destination")
            self.assertEqual(replace.call_count, 2)
        with patch.object(M.os, "replace", side_effect=PermissionError()) as replace, patch.object(M.time, "sleep"):
            with self.assertRaises(PermissionError):
                M.StorageRuntime.atomic_replace("source", "destination")
            self.assertEqual(replace.call_count, 12)

    def test_selected_symlink_names_are_preserved_for_frame_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = fixture(Path(tmp), panels=1)
            original = Path(project.sessions[0].panels[0].lights[0])
            alias = original.with_name("000_selected_name.fit")
            try:
                alias.symlink_to(original)
            except OSError:
                self.skipTest("Symlink privilege unavailable")
            project.sessions[0].panels[0].lights = [str(alias)]
            builder = M.SirilCommandBuilder(project)
            builder.build(); builder.storage_bundle.write()
            runtime = runtime_for(builder.storage_bundle)
            group = builder.storage_bundle.plan["inputs"][0]
            runtime.prepare_inputs([group["directory"]])
            self.assertTrue(Path(group["directory"], alias.name).exists())
            self.assertFalse(Path(group["directory"], original.name).exists())
            for name, record in runtime.state["owned"].items():
                runtime.validate_owned(name, record)

    def test_every_consumer_failure_retains_existing_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = fixture(Path(tmp), panels=1)
            for index in range(len(bundle.stages)):
                with self.subTest(stage=bundle.stages[index]["label"]):
                    builder = M.SirilCommandBuilder(project)
                    builder.build(); builder.storage_bundle.write()
                    runtime = runtime_for(builder.storage_bundle)
                    target = runtime.plan["stages"][index]
                    previous = runtime.iface.cmd
                    saved = []
                    def command(line):
                        if runtime.current is target and line == target["commands"][-1]:
                            saved.extend(Path(n) for n in runtime.state["owned"] if Path(n).exists())
                            raise RuntimeError("checkpoint consumer failure")
                        previous(line)
                    runtime.iface.cmd = command
                    with self.assertRaisesRegex(RuntimeError, "checkpoint consumer"):
                        runtime.run()
                    self.assertTrue(all(p.exists() for p in saved))
                    self.assertFalse(runtime.receipt.exists())

    def test_configuration_change_blocks_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, _ = fixture(Path(tmp), panels=1)
            first = runtime_for(bundle, fail="seqsubsky")
            with self.assertRaises(RuntimeError):
                first.run()
            resumed = runtime_for(bundle)
            previous = resumed.iface.get_siril_config
            resumed.iface.get_siril_config = lambda group, key: True if key == "force_16bit" else previous(group, key)
            with self.assertRaisesRegex(ValueError, "settings changed"):
                resumed.run()
            self.assertEqual(len(list(bundle.root.rglob("pp_light_*.fit"))), 3)

    def test_locked_cleanup_is_deferred_and_stays_in_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, bundle, _ = fixture(Path(tmp), panels=1)
            runtime = runtime_for(bundle)
            stage = bundle.stages[0]
            runtime.current = stage
            runtime.iface.cmd(stage["commands"][-1])
            runtime.remember(stage)
            owned = next(iter(runtime.state["owned"]))
            original_unlink = Path.unlink
            def locked(path, *args, **kwargs):
                if str(path) == owned:
                    raise PermissionError("held by another application")
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", locked):
                runtime.remove([owned])
            self.assertTrue(Path(owned).exists())
            self.assertIn(owned, runtime.state["owned"])
            self.assertGreater(runtime.state["live_bytes"], 0)

    def test_library_string_expansion_matches_siril(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = fixture(Path(tmp), panels=1)
            project.use_master_library = True
            project.sessions[0].master_bias = None
            for name in project.sessions[0].panels[0].flats:
                with fits.open(name, mode="update") as hdus:
                    hdus[0].header["INSTRUME"] = " Test Camera "
            builder = M.SirilCommandBuilder(project)
            builder.build();builder.storage_bundle.write()
            runtime = runtime_for(builder.storage_bundle)
            master = Path(tmp) / "Test_Camera_BIAS_G100.fit"
            fits.writeto(master, np.zeros((16,24), dtype="float32"))
            settings = {"gui_prepro.bias_lib": str(Path(tmp) / "$INSTRUME:%s$_BIAS_G$GAIN:%d$.fit")}
            names, unresolved = runtime.library_dependencies(settings)
            self.assertIn(str(master), names)
            # No dark template was supplied: it must disable uncertain reuse/resume.
            self.assertTrue(unresolved)

    def test_override_change_requires_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = fixture(Path(tmp), panels=1)
            before = M.LowDiskMosaicPlan.project_signature(project)
            project.sessions[0].panels[0].master_bias = "different.fit"
            self.assertNotEqual(before, M.LowDiskMosaicPlan.project_signature(project))


class StorageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = M.QtWidgets.QApplication.instance() or M.QtWidgets.QApplication([])

    def test_storage_controls_persist_and_busy_mode_preserves_abort(self):
        with patch.object(M, "s", None):
            widget = M.ProjectWidget()
        try:
            self.assertEqual(widget.cmb_storage.currentData(), "keep_all")
            widget.chk_mosaic_enabled.setChecked(True)
            widget.cmb_storage.setCurrentIndex(1)
            widget.cmb_storage_compression.setCurrentIndex(1)
            widget.sp_storage_reserve.setValue(32)
            widget.push_to_model()
            self.assertEqual(widget.project.storage_policy, "min_disk")
            self.assertEqual(widget.project.low_disk_compression, "gzip2")
            self.assertEqual(widget.project.storage_reserve_gib, 32)
            self.assertFalse(widget.cb_compress.isEnabled())
            widget._set_storage_busy(True)
            self.assertTrue(widget.btn_abort.isEnabled())
            self.assertFalse(widget.btn_build_script.isEnabled())
            self.assertFalse(widget.action_open.isEnabled())
            widget._set_storage_busy(False)
            self.assertTrue(widget.btn_build_script.isEnabled())
            self.assertFalse(widget.btn_abort.isEnabled())
        finally:
            widget.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
