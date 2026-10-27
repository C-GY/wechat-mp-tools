"""Run without importing app.py or starting any business/database services."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.desktop_diagnostics import DesktopDiagnostics


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def fire(self, *args):
        for callback in self.handlers:
            callback(*args)


def snapshot(pid=1, created=1, private=100):
    return {'system': {'physical_available_bytes': 1000, 'committed_bytes': 2000, 'commit_limit_bytes': 5000},
            'processes': [{'pid': pid, 'created_filetime': created, 'name': 'msedgewebview2.exe', 'private_bytes': private}]}


class DesktopDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make(self, **kwargs):
        diagnostics = DesktopDiagnostics(self.root, **kwargs)
        self.addCleanup(diagnostics.stop)
        return diagnostics

    def records(self, name='desktop_events.jsonl'):
        path = self.root / name
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_daemon_starts_once_and_stops_with_application(self):
        sampled = threading.Event()

        def sampler():
            sampled.set()
            return snapshot()

        diagnostics = self.make(sampler=sampler, interval=3600)
        diagnostics.start()
        worker = diagnostics._thread
        diagnostics.start()
        self.assertIs(worker, diagnostics._thread)
        self.assertTrue(sampled.wait(2))
        self.assertTrue(worker.daemon)
        diagnostics.stop()
        self.assertFalse(worker.is_alive())
        self.assertEqual([r['event'] for r in self.records()], ['monitor_started', 'monitor_stopped'])

    def test_failure_keeps_last_sample_after_later_sampling_and_drops_sensitive_fields(self):
        diagnostics = self.make(sampler=Mock(side_effect=[snapshot(private=999), snapshot(private=10)]))
        diagnostics.sample()
        diagnostics._process_failed(None, SimpleNamespace(ProcessFailedKind='RenderProcessExited',
            Reason='OutOfMemory', ExitCode=-1, ProcessDescription='SECRET_SENTINEL',
            FrameInfosForFailedProcess=['https://example.com/?token=URL_SENTINEL']))
        diagnostics.sample()
        event = next(r for r in self.records() if r['event'] == 'webview_process_failed')
        self.assertEqual(event['Reason'], 'OutOfMemory')
        self.assertEqual(event['last_memory_sample']['processes'][0]['private_bytes'], 999)
        self.assertNotIn('SENTINEL', json.dumps(event))
        self.assertEqual(self.records('desktop_memory.jsonl')[-1]['processes'][0]['private_bytes'], 10)

    def test_pid_reuse_and_disappearance_are_recorded_as_changes_not_crash_diagnoses(self):
        diagnostics = self.make(sampler=Mock(side_effect=[snapshot(created=1), snapshot(created=2), {'processes': []}]))
        for _ in range(3):
            diagnostics.sample()
        events = self.records()
        self.assertEqual([r['event'] for r in events], ['process_tree_changed', 'process_tree_changed'])
        self.assertEqual(events[0]['appeared'], [1])
        self.assertEqual(events[0]['disappeared'], [1])

    def test_sampler_and_read_only_directory_failures_do_not_escape(self):
        diagnostics = self.make(sampler=Mock(side_effect=RuntimeError('password=SECRET_SENTINEL')))
        diagnostics.sample()
        self.assertNotIn('SENTINEL', json.dumps(self.records()))
        self.assertEqual(self.records()[0]['error_type'], 'RuntimeError')
        blocker = self.root / 'not-a-directory'
        blocker.write_text('preserve me', encoding='utf-8')
        blocked = DesktopDiagnostics(blocker, sampler=lambda: snapshot(), interval=3600)
        blocked.start()
        blocked.stop()
        self.assertEqual(blocker.read_text(encoding='utf-8'), 'preserve me')

    def test_thread_start_failure_still_allows_application_shutdown(self):
        diagnostics = self.make(sampler=lambda: snapshot())
        with patch('threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
            diagnostics.start()
        diagnostics.stop()
        self.assertEqual(self.records()[-1]['event'], 'monitor_stopped')
        self.assertTrue(any(r['event'] == 'monitor_start_failed' for r in self.records()))

    def test_rotation_bounds_history_and_retains_separate_failure_log(self):
        diagnostics = self.make(sampler=lambda: snapshot(), memory_bytes=1024, event_bytes=2048)
        for _ in range(100):
            diagnostics.sample()
        diagnostics._process_failed(None, SimpleNamespace(ProcessFailedKind='RenderProcessExited', Reason='OutOfMemory', ExitCode=1))
        files = list(self.root.glob('desktop_memory.jsonl*'))
        self.assertEqual(len(files), 4)
        self.assertTrue(all(p.stat().st_size <= 1024 for p in files))
        self.assertEqual(self.records()[-1]['event'], 'webview_process_failed')

    def test_native_hooks_are_dispatched_to_ui_and_not_duplicated_after_reload(self):
        core = SimpleNamespace(ProcessFailed=Event(), NavigationStarting=Event(), NavigationCompleted=Event(),
            BrowserProcessId=100, Environment=SimpleNamespace(BrowserProcessExited=Event(), BrowserVersionString='154.0.1.2',
                ProcessInfosChanged=Event(), GetProcessInfos=lambda: [SimpleNamespace(ProcessId=1, Kind='Renderer')]))
        control = SimpleNamespace(CoreWebView2=core, CoreWebView2InitializationCompleted=Event())
        window = SimpleNamespace(events=SimpleNamespace(shown=Event(), loaded=Event()), native=SimpleNamespace(webview=control))
        diagnostics = self.make(sampler=lambda: snapshot())
        queued = []
        with patch.object(diagnostics, '_dispatch', side_effect=lambda _, callback: queued.append(callback)), patch('sys.platform', 'win32'):
            diagnostics.attach_window(window)
            window.events.shown.fire()
            self.assertEqual(len(core.ProcessFailed.handlers), 0)
            queued.pop(0)()
            window.events.loaded.fire()
            queued.pop(0)()
            self.assertEqual(len(core.ProcessFailed.handlers), 1)
            diagnostics.sample()
            self.assertEqual(self.records('desktop_memory.jsonl')[-1]['processes'][0]['role'], 'Renderer')
            core.ProcessFailed.fire(core, SimpleNamespace(ProcessFailedKind='RenderProcessExited', Reason='OutOfMemory', ExitCode=1))
            core.NavigationCompleted.fire(core, SimpleNamespace(NavigationId=2, IsSuccess=True, WebErrorStatus='Unknown'))
        events = self.records()
        self.assertEqual(sum(r['event'] == 'webview_hook_attached' for r in events), 1)
        self.assertTrue(any(r.get('Reason') == 'OutOfMemory' for r in events))
        self.assertEqual(events[-1]['event'], 'navigation_completed')

    def test_desktop_entry_wires_recording_without_importing_real_application(self):
        root = Path(__file__).resolve().parents[1]
        diagnostics = Mock()
        window = SimpleNamespace(events=SimpleNamespace(closing=Event()))
        fake_webview = SimpleNamespace(create_window=Mock(return_value=window), start=Mock())
        with patch.dict(sys.modules, {'app': SimpleNamespace(app=Mock()), 'webview': fake_webview}), \
             patch('backend.runtime.configure_runtime'), patch('backend.runtime.write_startup_error') as startup_error, \
             patch('backend.desktop_diagnostics.start_desktop_diagnostics', return_value=diagnostics) as start, \
             patch('backend.desktop_diagnostics.stop_desktop_diagnostics') as stop, \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=0)), \
             patch('socket.socket'), patch('urllib.request.urlopen'), patch('time.sleep'), \
             patch('multiprocessing.freeze_support'):
            runpy.run_path(str(root / 'main.py'), run_name='__main__')
        start.assert_called_once()
        diagnostics.attach_window.assert_called_once_with(window)
        stop.assert_called_once()
        startup_error.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native memory API')
    def test_real_windows_sampler_reads_only_current_process_tree(self):
        from backend.windows_memory import WindowsMemorySampler
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            sample = WindowsMemorySampler()()
            processes = {p['pid']: p for p in sample['processes']}
            self.assertIn(os.getpid(), processes)
            self.assertIn(child.pid, processes)
            self.assertGreater(processes[os.getpid()]['private_bytes'], 0)
            self.assertGreater(sample['system']['physical_total_bytes'], sample['system']['physical_available_bytes'])
            self.assertGreater(sample['system']['commit_limit_bytes'], sample['system']['committed_bytes'])
            self.assertNotIn(os.getppid(), processes)
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
