import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_storage_policy import M, fixture, runtime_for


class DiscardRunTests(unittest.TestCase):
    def failed_bundle(self, root, cancelled=False):
        project, bundle, _ = fixture(root, panels=1)
        runtime = runtime_for(bundle, **({'cancel': 'seqsubsky'} if cancelled else {'fail': 'seqsubsky'}))
        with self.assertRaises(InterruptedError if cancelled else RuntimeError):
            runtime.run()
        return project, bundle, runtime

    def test_discards_failed_and_cancelled_bundles_preserving_sources_and_products(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory(prefix='OSC discard ') as tmp:
                project, bundle, runtime = self.failed_bundle(Path(tmp), cancelled)
                keep = list(bundle.plan['protected']) + list(runtime.state['retained'])
                final = Path(bundle.plan['final'])
                final.write_bytes(b'retained final')
                keep.append(str(final))
                before = {name: Path(name).read_bytes() for name in keep}
                other = bundle.folder.parent / 'other data'
                other.mkdir()
                (other / 'keep.txt').write_text('keep')
                runs = M._failed_low_disk_runs(project.working_dir)
                self.assertEqual([(p, status) for p, status, size in runs],
                                 [(bundle.folder, 'cancelled' if cancelled else 'failed')])
                size = M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
                self.assertGreater(size, 0)
                self.assertFalse(bundle.folder.exists())
                self.assertEqual(before, {name: Path(name).read_bytes() for name in keep})
                self.assertEqual((other / 'keep.txt').read_text(), 'keep')
                self.assertTrue((bundle.folder.parent / 'active.lock').exists())

    def test_active_project_lock_blocks_discard_even_with_stale_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, runtime = self.failed_bundle(Path(tmp))
            runtime.lock()
            iface = Mock()
            try:
                with self.assertRaisesRegex(RuntimeError, 'Another managed run'):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder, iface=iface)
                iface.get_siril_wd.assert_not_called()
                iface.cmd.assert_not_called()
                self.assertTrue(bundle.manifest_path.exists())
                self.assertTrue(list(bundle.root.rglob('pp_light_*.fit')))
            finally:
                runtime.lock_file.close()

    def test_status_is_rechecked_after_acquiring_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, runtime = self.failed_bundle(Path(tmp))
            original_lock = M.StorageRuntime.lock

            def change_status(controller):
                original_lock(controller)
                state = json.loads(runtime.state_path.read_text())
                state['status'] = 'running'
                runtime.state_path.write_text(json.dumps(state))

            with patch.object(M.StorageRuntime, 'lock', change_status):
                with self.assertRaisesRegex(ValueError, 'Only failed or cancelled'):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
            runtime.lock()  # A rejected deletion must release its lock.
            runtime.lock_file.close()
            self.assertTrue(bundle.manifest_path.exists())

    def test_running_complete_and_invalid_bundles_are_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, runtime = self.failed_bundle(Path(tmp))
            for status in ('running', 'starting', 'complete', 'unknown'):
                with self.subTest(status=status):
                    state = json.loads(runtime.state_path.read_text())
                    state['status'] = status
                    runtime.state_path.write_text(json.dumps(state))
                    self.assertEqual(M._failed_low_disk_runs(project.working_dir), [])
            runtime.state_path.write_text('not JSON')
            self.assertEqual(M._failed_low_disk_runs(project.working_dir), [])

    def test_wrong_manifest_or_outside_directory_is_refused(self):
        for key in ('schema', 'id', 'work', 'scratch'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                project, bundle, _ = self.failed_bundle(Path(tmp))
                plan = json.loads(bundle.manifest_path.read_text())
                plan[key] = 999 if key == 'schema' else str(Path(tmp) / 'outside')
                bundle.manifest_path.write_text(json.dumps(plan))
                self.assertEqual(M._failed_low_disk_runs(project.working_dir), [])
                with self.assertRaises(ValueError):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
                with self.assertRaises(ValueError):
                    M._discard_failed_low_disk_run(project.working_dir, Path(tmp))
                self.assertTrue(bundle.folder.exists())

    def test_current_inputs_and_manifest_sources_and_retained_products_inside_run_are_refused(self):
        for kind in ('current', 'relative', 'original', 'library', 'final', 'promoted'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                project, bundle, runtime = self.failed_bundle(Path(tmp))
                source = bundle.root / 'keep.fit'
                source.write_bytes(b'important source')
                protected = []
                plan = json.loads(bundle.manifest_path.read_text())
                state = json.loads(runtime.state_path.read_text())
                if kind == 'current':
                    project.sessions[0].panels[0].ha_oiii.lights = [str(source)]
                    protected = M._low_disk_project_paths(project)
                elif kind == 'relative':
                    protected = [str(source.relative_to(Path(project.working_dir)))]
                elif kind == 'original':
                    plan['protected'].append(str(source))
                elif kind == 'library':
                    state['dependencies'][str(source)] = {}
                elif kind == 'final':
                    plan['final'] = str(source)
                else:
                    plan['stages'][0]['promote'].append([str(source), str(source)])
                bundle.manifest_path.write_text(json.dumps(plan))
                runtime.state_path.write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, 'configured input or retained output'):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder, protected)
                self.assertEqual(source.read_bytes(), b'important source')

    def test_directory_links_and_metadata_links_are_refused_but_file_aliases_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            target = Path(tmp) / 'external'
            target.mkdir()
            source = target / 'source.fit'
            source.write_bytes(b'preserve source')
            link = bundle.root / 'directory-link'
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'Symlinks unavailable: {exc}')
            self.assertEqual(M._failed_low_disk_runs(project.working_dir), [])
            with self.assertRaisesRegex(ValueError, 'directory link'):
                M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
            link.unlink()
            saved = target / 'manifest.json'
            saved.write_bytes(bundle.manifest_path.read_bytes())
            bundle.manifest_path.unlink()
            bundle.manifest_path.symlink_to(saved)
            self.assertEqual(M._failed_low_disk_runs(project.working_dir), [])
            bundle.manifest_path.unlink()
            bundle.manifest_path.write_bytes(saved.read_bytes())
            (bundle.root / 'file-alias.fit').symlink_to(source)
            M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
            self.assertEqual(source.read_bytes(), b'preserve source')

    def test_locked_scratch_keeps_metadata_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            with patch.object(M.shutil, 'rmtree', side_effect=PermissionError('File in use')):
                with self.assertRaises(PermissionError):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
            self.assertTrue(bundle.manifest_path.exists())
            self.assertTrue((bundle.folder / 'state.json').exists())
            self.assertEqual(len(M._failed_low_disk_runs(project.working_dir)), 1)
            M._discard_failed_low_disk_run(project.working_dir, bundle.folder)
            self.assertFalse(bundle.folder.exists())

    def test_siril_release_happens_under_lock_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, runtime = self.failed_bundle(Path(tmp))
            iface = Mock()
            iface.get_siril_wd.return_value = str(bundle.root)

            def command(line):
                self.assertTrue(bundle.manifest_path.exists())
                self.assertTrue(list(bundle.root.rglob('pp_light_*.fit')))
                with self.assertRaisesRegex(RuntimeError, 'Another managed run'):
                    runtime.lock()

            iface.cmd.side_effect = command
            M._discard_failed_low_disk_run(project.working_dir, bundle.folder, iface=iface)
            self.assertEqual([c.args[0] for c in iface.cmd.call_args_list],
                             ['close', 'cd ' + M.LowDiskMosaicPlan.quote(Path(project.working_dir).resolve())])
            self.assertFalse(bundle.folder.exists())

    def test_unrelated_siril_working_directory_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            iface = Mock()
            iface.get_siril_wd.return_value = tmp
            M._discard_failed_low_disk_run(project.working_dir, bundle.folder, iface=iface)
            iface.cmd.assert_not_called()

    def test_release_failure_does_not_delete_recovery_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            iface = Mock()
            iface.get_siril_wd.return_value = str(bundle.root)
            iface.cmd.side_effect = RuntimeError('Siril could not release the sequence')
            with patch.object(M.shutil, 'rmtree') as remove:
                with self.assertRaisesRegex(RuntimeError, 'could not release'):
                    M._discard_failed_low_disk_run(project.working_dir, bundle.folder, iface=iface)
            remove.assert_not_called()
            self.assertTrue(bundle.manifest_path.exists())
            self.assertTrue(list(bundle.root.rglob('pp_light_*.fit')))

    @unittest.skipUnless(os.name == 'nt', 'Windows current-directory locking')
    def test_native_windows_current_directory_lock_is_released_before_discard(self):
        with tempfile.TemporaryDirectory(prefix='OSC locked directory ') as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            locked = bundle.root / 'locked process'
            locked.mkdir()
            child_code = (
                "import json,os,sys\n"
                "print(json.dumps(os.getcwd()),flush=True)\n"
                "for line in sys.stdin:\n"
                "    request=json.loads(line)\n"
                "    if request.get('cd'): os.chdir(request['cd'])\n"
                "    print(json.dumps(os.getcwd()),flush=True)\n")
            child = subprocess.Popen([sys.executable, '-u', '-c', child_code], cwd=project.working_dir,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(Path(json.loads(child.stdout.readline())).resolve(), Path(project.working_dir).resolve())

                class Interface:
                    def request(self, data):
                        child.stdin.write(json.dumps(data) + '\n')
                        child.stdin.flush()
                        return json.loads(child.stdout.readline())

                    def get_siril_wd(self):
                        return self.request({})

                    def cmd(self, command):
                        args = shlex.split(command)
                        self.request({'cd': args[1]} if args[0] == 'cd' else {})

                iface = Interface()
                # Siril starts outside the bundle, then enters it while running.
                iface.request({'cd': str(locked)})
                self.assertEqual(Path(iface.get_siril_wd()).resolve(), locked.resolve())
                with self.assertRaises(PermissionError) as error:
                    locked.rmdir()
                self.assertEqual(error.exception.winerror, 32)
                M._discard_failed_low_disk_run(project.working_dir, bundle.folder, iface=iface)
                self.assertFalse(bundle.folder.exists())
                self.assertEqual(Path(iface.get_siril_wd()).resolve(), Path(project.working_dir).resolve())
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

    def test_api_failure_releases_run_even_before_manifest_is_loaded_by_finished_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, bundle, _ = self.failed_bundle(Path(tmp))
            iface = Mock()
            iface.get_siril_wd.return_value = str(bundle.root)
            worker = Mock(error='Injected processing failure')
            widget = SimpleNamespace(_set_storage_busy=Mock(), lbl_run_mode=Mock(),
                                     _on_storage_status=Mock(), siril=SimpleNamespace(iface=iface, log=Mock()))
            with patch.object(M, '_StorageThread', return_value=worker), \
                 patch.object(M.QtWidgets.QMessageBox, 'critical') as critical:
                M.ProjectWidget._run_storage_api(widget, bundle.manifest_path)
                worker.finished.connect.call_args.args[0]()
            self.assertEqual([c.args[0] for c in iface.cmd.call_args_list],
                             ['close', 'cd ' + M.LowDiskMosaicPlan.quote(Path(project.working_dir).resolve())])
            self.assertIn('Injected processing failure', critical.call_args.args[2])
            self.assertTrue(bundle.folder.exists())

    def test_api_failure_does_not_release_another_active_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project, bundle, runtime = self.failed_bundle(Path(tmp))
            iface = Mock()
            worker = Mock(error='Another managed run is using this project')
            widget = SimpleNamespace(_set_storage_busy=Mock(), lbl_run_mode=Mock(),
                                     _on_storage_status=Mock(), siril=SimpleNamespace(iface=iface, log=Mock()))
            runtime.lock()
            try:
                with patch.object(M, '_StorageThread', return_value=worker), \
                     patch.object(M.QtWidgets.QMessageBox, 'critical'):
                    M.ProjectWidget._run_storage_api(widget, bundle.manifest_path)
                    worker.finished.connect.call_args.args[0]()
                iface.get_siril_wd.assert_not_called()
                iface.cmd.assert_not_called()
            finally:
                runtime.lock_file.close()

    def test_ui_requires_selection_and_explicit_confirmation_and_preserves_dirty_state(self):
        for accepted, confirmed in ((False, False), (True, False), (True, True)):
            with self.subTest(accepted=accepted, confirmed=confirmed), tempfile.TemporaryDirectory() as tmp:
                project, bundle, _ = self.failed_bundle(Path(tmp))
                widget = SimpleNamespace(project=project, push_to_model=Mock(), status_message=Mock(),
                                         _dirty=True, _suspend_dirty=False, _storage_busy=False)

                def select(_parent, _title, _prompt, labels, _current, editable):
                    self.assertFalse(editable)
                    return labels[0], accepted

                yes, no = M.QtWidgets.QMessageBox.StandardButton.Yes, M.QtWidgets.QMessageBox.StandardButton.No
                with patch.object(M.QtWidgets.QInputDialog, 'getItem', side_effect=select), \
                     patch.object(M.QtWidgets.QMessageBox, 'warning', return_value=yes if confirmed else no) as warning, \
                     patch.object(M.QtWidgets.QMessageBox, 'information'), \
                     patch.object(M.QtWidgets.QMessageBox, 'critical') as critical:
                    M.ProjectWidget.discard_failed_low_disk_run(widget)
                self.assertEqual(bundle.folder.exists(), not (accepted and confirmed))
                self.assertTrue(widget._dirty)
                self.assertFalse(widget._suspend_dirty)
                critical.assert_not_called()
                if accepted:
                    self.assertIn('lose the ability to resume', warning.call_args.args[2])
                    self.assertEqual(warning.call_args.args[-1], no)
                else:
                    warning.assert_not_called()


if __name__ == '__main__':
    unittest.main()
