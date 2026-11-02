import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from test_storage_policy import M, fixture, runtime_for


def cleanup_widget(project):
    return SimpleNamespace(project=project, push_to_model=Mock(), status_message=Mock(),
                           _dirty=True, _suspend_dirty=False, sessions_list=Mock())


class SessionCleanupBoundaryTests(unittest.TestCase):
    def test_root_parent_absolute_escape_and_managed_bundle_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = root / 'work'
            work.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            for folder in ('.', '..', '../outside', str(outside), '.osc_low_disk', '.osc_low_disk/run'):
                with self.subTest(folder=folder), self.assertRaises(ValueError):
                    M._session_cleanup_path(work, M.Session(name='Session 1', work_subdir=folder))
            self.assertTrue(outside.exists())

    def test_valid_nested_and_missing_directories_remain_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            for folder in ('Session 1', 'nights/Night One', str(work / 'absolute session')):
                with self.subTest(folder=folder):
                    target = M._session_cleanup_path(work, M.Session(name='Session 1', work_subdir=folder))
                    self.assertTrue(target.is_relative_to(work))
                    self.assertNotEqual(target, work)

    def test_regular_file_cannot_be_used_as_a_session_cleanup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'original.fit'
            path.write_bytes(b'original')
            with self.assertRaisesRegex(ValueError, 'not a directory'):
                M._session_cleanup_path(tmp, M.Session(name='original.fit'))
            self.assertEqual(path.read_bytes(), b'original')

    def test_directory_aliases_cannot_redirect_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work, outside = root / 'work', root / 'outside'
            work.mkdir(); outside.mkdir()
            source = outside / 'original.fit'
            source.write_bytes(b'original')
            link = work / 'Session 1'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'Symlinks unavailable: {exc}')
            try:
                for folder in ('Session 1', 'Session 1/subfolder'):
                    with self.subTest(folder=folder), self.assertRaises(ValueError):
                        M._session_cleanup_path(work, M.Session(name='Session 1', work_subdir=folder))
                self.assertEqual(source.read_bytes(), b'original')
            finally:
                link.unlink()

    def test_remove_session_refuses_escape_without_removing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = M.Project(working_dir=tmp, sessions=[M.Session(name='Session 1', work_subdir='..')])
            widget = cleanup_widget(project)
            widget.sessions_list.currentRow.return_value = 0
            with patch.object(M.QtWidgets, 'QMessageBox') as box, patch.object(M.shutil, 'rmtree') as remove:
                M.ProjectWidget.remove_session(widget)
                remove.assert_not_called()
                box.question.assert_not_called()
                box.warning.assert_called_once()
            self.assertEqual(len(project.sessions), 1)
            widget.sessions_list.takeItem.assert_not_called()

    def test_remove_all_preflights_every_session_before_deleting_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            (work / 'Session 1').mkdir()
            project = M.Project(working_dir=tmp, sessions=[M.Session(name='Session 1'),
                                                         M.Session(name='Session 2', work_subdir='..')])
            widget = cleanup_widget(project)
            with patch.object(M.QtWidgets, 'QMessageBox') as box, patch.object(M.shutil, 'rmtree') as remove:
                M.ProjectWidget.remove_all_sessions_data(widget)
                remove.assert_not_called()
                box.warning.assert_called_once()
            self.assertTrue((work / 'Session 1').exists())
            self.assertTrue(widget._dirty)
            self.assertFalse(widget._suspend_dirty)

    def test_remove_session_rechecks_confirmation_before_removing_data_or_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = M.Project(working_dir=tmp, sessions=[M.Session(name='Session 1')])
            (Path(tmp) / 'Session 1').mkdir()
            widget = cleanup_widget(project)
            widget.sessions_list.currentRow.return_value = 0
            with patch.object(M.QtWidgets, 'QMessageBox') as box, patch.object(M.shutil, 'rmtree') as remove:
                def confirm(*args):
                    project.sessions[0].work_subdir = '..'
                    return box.StandardButton.Yes
                box.question.side_effect = confirm
                M.ProjectWidget.remove_session(widget)
                remove.assert_not_called()
                box.warning.assert_called_once()
            self.assertEqual(len(project.sessions), 1)
            widget.sessions_list.takeItem.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction containment')
    def test_native_junction_is_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix='OSC junction check ') as tmp:
            root = Path(tmp).resolve()
            work, outside = root / 'work', root / 'outside'
            work.mkdir(); outside.mkdir()
            source = outside / 'original.fit'
            source.write_bytes(b'original')
            link = work / 'Session 1'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest(f'Junction creation unavailable: {result.stderr}')
            try:
                with self.assertRaises(ValueError):
                    M._session_cleanup_path(work, M.Session(name='Session 1'))
                self.assertEqual(source.read_bytes(), b'original')
            finally:
                self.assertTrue(link.absolute().is_relative_to(root))
                self.assertTrue(link.resolve().is_relative_to(root))
                link.rmdir()  # Remove the junction itself, without traversing its target.

    def test_remove_all_rechecks_after_confirmation_and_supports_normal_cleanup(self):
        for change in (False, True):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp).resolve()
                folder = work / 'Session 1'
                folder.mkdir()
                (folder / 'temporary.txt').write_text('temporary')
                project = M.Project(working_dir=tmp, sessions=[M.Session(name='Session 1')])
                widget = cleanup_widget(project)
                with patch.object(M.QtWidgets, 'QMessageBox') as box, \
                     patch.object(M.QtWidgets, 'QCheckBox') as checkbox:
                    def confirm():
                        if change:
                            project.sessions[0].work_subdir = '..'
                        return box.StandardButton.Yes
                    box.return_value.exec.side_effect = confirm
                    checkbox.return_value.isChecked.return_value = False
                    M.ProjectWidget.remove_all_sessions_data(widget)
                    if change:
                        box.warning.assert_called_once()
                self.assertEqual(folder.exists(), change)


class ConsoleCaptureTests(unittest.TestCase):
    def test_nul_terminated_growing_logs_are_captured_once_in_app_and_exported_worker(self):
        for exported in (False, True):
            with self.subTest(exported=exported), tempfile.TemporaryDirectory() as tmp:
                _, bundle, _ = fixture(Path(tmp), panels=1)
                if exported:
                    worker = runpy.run_path(str(bundle.folder / 'worker.py'))
                    runtime = worker['StorageRuntime'](bundle.manifest_path, None, reserve_bytes=0)
                else:
                    runtime = runtime_for(bundle)
                runtime.iface = Mock()
                runtime.iface.get_siril_log.side_effect = [
                    'first\n\0', 'first\nsecond\n\0', 'first\nsecond\nthird\n\0',
                    'first\nsecond\nthird\n\0', '', 'after clear\n\0']
                for _ in range(6):
                    runtime.capture_log()
                text = runtime.log_path.read_text()
                for message in ('first', 'second', 'third', 'after clear'):
                    self.assertEqual(text.count(message), 1)
                self.assertNotIn('\0', text)
                self.assertEqual(runtime.last_log, 'after clear\n')


class AtomicProjectSaveTests(unittest.TestCase):
    def test_success_round_trips_json_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix='OSC save ') as tmp:
            path = Path(tmp) / 'project.json'
            path.write_bytes(b'{"previous":true}')
            data = {'name': 'Veil', 'sessions': [{'name': 'Session 1'}]}
            M._write_project_json(path, data)
            self.assertEqual(json.loads(path.read_text()), data)
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

    def test_interrupted_write_flush_or_replace_preserves_previous_project(self):
        for failure in ('write', 'flush', 'replace', 'serialize'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'project.json'
                previous = b'{"name":"previous project"}'
                path.write_bytes(previous)
                original_open = Path.open

                class InterruptedWriter:
                    def __init__(self, stream): self.stream = stream
                    def __enter__(self): return self
                    def __exit__(self, *args): self.stream.close()
                    def write(self, text):
                        self.stream.write(text[:5])
                        raise OSError('Interrupted project write')

                def open_file(p, *args, **kwargs):
                    stream = original_open(p, *args, **kwargs)
                    return InterruptedWriter(stream) if failure == 'write' and p.suffix == '.tmp' else stream

                data = {'name': 'new project'} if failure != 'serialize' else {'invalid': object()}
                with patch.object(Path, 'open', open_file), \
                     patch.object(M.os, 'fsync', side_effect=OSError('Flush failed') if failure == 'flush' else None), \
                     patch.object(M.os, 'replace', side_effect=PermissionError('Replace locked')) as replace, \
                     patch.object(M.time, 'sleep'):
                    with self.assertRaises((OSError, TypeError)):
                        M._write_project_json(path, data)
                    if failure != 'replace':
                        replace.assert_not_called()
                self.assertEqual(path.read_bytes(), previous)
                self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

    def test_transient_replace_lock_retries_and_then_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            path.write_text('{"previous":true}')
            original_replace = M.os.replace
            calls = []
            def replace(source, destination):
                calls.append(source)
                if len(calls) == 1:
                    raise PermissionError('Transient lock')
                return original_replace(source, destination)
            with patch.object(M.os, 'replace', side_effect=replace), patch.object(M.time, 'sleep'):
                M._write_project_json(path, {'new': True})
            self.assertEqual(len(calls), 2)
            self.assertEqual(json.loads(path.read_text()), {'new': True})

    def test_save_ui_keeps_dirty_state_and_previous_data_when_flush_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            before = b'{"name":"previous"}'
            path.write_bytes(before)
            widget = SimpleNamespace(project=M.Project(project_file=str(path)), push_to_model=Mock(),
                                     _dirty=True, status_message=Mock())
            with patch.object(M.os, 'fsync', side_effect=OSError('Interrupted save')), \
                 patch.object(M.QtWidgets.QMessageBox, 'critical') as critical:
                self.assertFalse(M.ProjectWidget.save_project(widget))
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(widget._dirty)
            widget.status_message.emit.assert_not_called()
            critical.assert_called_once()


def abort_widget(proc, reader):
    widget = SimpleNamespace(_managed_manifest=None, _storage_busy=False, _proc=proc, _reader=reader,
                             btn_abort=Mock(), lbl_run_mode=Mock())
    widget._escalate_abort = MethodType(M.ProjectWidget._escalate_abort, widget)
    widget._kill_abort = MethodType(M.ProjectWidget._kill_abort, widget)
    return widget


class CliAbortTests(unittest.TestCase):
    def test_process_finishing_or_changing_during_confirmation_is_not_stopped(self):
        for change in ('finished', 'replaced', 'declined'):
            with self.subTest(change=change):
                old, new = Mock(), Mock()
                old.poll.return_value = None
                widget = abort_widget(old, Mock())
                yes, no = M.QtWidgets.QMessageBox.StandardButton.Yes, M.QtWidgets.QMessageBox.StandardButton.No
                def confirm(*args):
                    if change == 'finished':
                        old.poll.return_value = 0
                        widget._proc = None
                    elif change == 'replaced':
                        widget._proc = new
                    return no if change == 'declined' else yes
                with patch.object(M.QtWidgets.QMessageBox, 'question', side_effect=confirm), \
                     patch.object(M.QtCore.QTimer, 'singleShot') as timer:
                    M.ProjectWidget.abort_siril(widget)
                old.send_signal.assert_not_called()
                new.send_signal.assert_not_called()
                timer.assert_not_called()
                widget.btn_abort.setEnabled.assert_not_called()

    def test_abort_keeps_reader_draining_and_escalates_even_if_interrupt_fails(self):
        for interrupt_error in (None, OSError('Signal unavailable')):
            with self.subTest(interrupt_error=interrupt_error):
                proc, reader = Mock(), Mock()
                proc.poll.return_value = None
                proc.send_signal.side_effect = interrupt_error
                widget = abort_widget(proc, reader)
                pending = []
                with patch.object(M.QtWidgets.QMessageBox, 'question', return_value=M.QtWidgets.QMessageBox.StandardButton.Yes), \
                     patch.object(M.QtCore.QTimer, 'singleShot', side_effect=lambda ms, callback: pending.append(callback)):
                    M.ProjectWidget.abort_siril(widget)
                    reader.stop.assert_not_called()
                    pending.pop(0)()
                    proc.terminate.assert_called_once()
                    pending.pop(0)()
                    proc.kill.assert_called_once()
                self.assertIs(widget._proc, proc)  # The finished reader owns teardown.

    def test_delayed_callbacks_cannot_terminate_a_newer_run(self):
        for replace_after_terminate in (False, True):
            with self.subTest(replace_after_terminate=replace_after_terminate):
                old, new = Mock(), Mock()
                old.poll.return_value = None
                widget = abort_widget(old, Mock())
                pending = []
                with patch.object(M.QtWidgets.QMessageBox, 'question', return_value=M.QtWidgets.QMessageBox.StandardButton.Yes), \
                     patch.object(M.QtCore.QTimer, 'singleShot', side_effect=lambda ms, callback: pending.append(callback)):
                    M.ProjectWidget.abort_siril(widget)
                    if replace_after_terminate:
                        pending.pop(0)()
                    widget._proc = new
                    pending.pop(0)()
                new.terminate.assert_not_called()
                new.kill.assert_not_called()
                old.kill.assert_not_called()
                self.assertEqual(old.terminate.call_count, int(replace_after_terminate))

    def test_managed_abort_still_uses_cancel_request_without_forced_termination(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / 'manifest.json'
            cancel = Path(tmp) / 'cancel.request'
            widget = abort_widget(Mock(), Mock())
            widget._managed_manifest, widget._storage_busy = manifest, True
            with patch.object(M, 'StorageRuntime') as runtime, patch.object(M.QtCore.QTimer, 'singleShot') as timer:
                runtime.return_value.safe_path.return_value = cancel
                M.ProjectWidget.abort_siril(widget)
            self.assertTrue(cancel.read_text().isdigit())
            timer.assert_not_called()
            widget._proc.send_signal.assert_not_called()

    def test_native_verbose_child_ignoring_stop_is_drained_until_killed(self):
        with tempfile.TemporaryDirectory(prefix='OSC CLI abort ') as tmp:
            child = subprocess.Popen(
                [sys.executable, '-u', '-c', "import sys\nwhile True:\n sys.stdout.write('x'*4096+'\\n');sys.stdout.flush()"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            ready = threading.Event()

            class Output:
                def readline(self):
                    line = child.stdout.readline()
                    if line: ready.set()
                    return line

            class Process:
                stdout = Output()
                send_signal = Mock()  # Ignore the initial stop request.
                terminate = Mock()  # Force the final kill path as well.
                def __getattr__(self, name): return getattr(child, name)

            proc = Process()
            reader = M._ProcReader(proc, Path(tmp) / 'cli.log')
            thread = threading.Thread(target=reader.run, daemon=True)
            widget = abort_widget(proc, reader)
            pending = []
            try:
                thread.start()
                self.assertTrue(ready.wait(timeout=5))
                with patch.object(M.QtWidgets.QMessageBox, 'question', return_value=M.QtWidgets.QMessageBox.StandardButton.Yes), \
                     patch.object(M.QtCore.QTimer, 'singleShot', side_effect=lambda ms, callback: pending.append(callback)):
                    M.ProjectWidget.abort_siril(widget)
                    self.assertFalse(reader._stop)
                    pending.pop(0)()
                    pending.pop(0)()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertIsNotNone(child.poll())
                self.assertGreater((Path(tmp) / 'cli.log').stat().st_size, 0)
            finally:
                if child.poll() is None: child.kill()
                child.wait(timeout=5)
                thread.join(timeout=5)
                child.stdout.close()


if __name__ == '__main__':
    unittest.main()
