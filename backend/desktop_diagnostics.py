"""In-process, bounded desktop diagnostics. Importing this module starts nothing."""
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from backend.runtime import app_dir


def _now():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _enum(value):
    text = str(value)
    return text if re.fullmatch(r'[A-Za-z0-9_]{1,64}', text) else 'Unknown'


class DesktopDiagnostics:
    def __init__(self, directory=None, sampler=None, interval=15, memory_bytes=8 * 1024 * 1024,
                 event_bytes=2 * 1024 * 1024, watchdog=False):
        self.directory = Path(directory) if directory is not None else app_dir() / 'data' / 'logs'
        self.session = uuid.uuid4().hex
        self._sampler = sampler
        self._interval = interval
        self._limits = {'desktop_memory.jsonl': memory_bytes, 'desktop_events.jsonl': event_bytes}
        self._loggers = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._started = False
        self._closed = False
        self._last_sample = None
        self._previous_processes = None
        self._process_roles = {}
        self._windows = []
        self._bound_controls = set()
        self._bindings = []  # Retain Python callbacks for .NET event delegates.
        self._watchdog_enabled = watchdog
        self._watchdog = None

    def _write(self, filename, event, **fields):
        # Diagnostics must never change whether the desktop app can run.
        try:
            with self._lock:
                if self._closed:
                    return
                logger = self._loggers.get(filename)
                if logger is None:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    handler = RotatingFileHandler(self.directory / filename, maxBytes=self._limits[filename],
                                                  backupCount=3, encoding='utf-8')
                    logger = logging.Logger('desktop.' + self.session + '.' + filename)
                    logger.propagate = False
                    logger.addHandler(handler)
                    self._loggers[filename] = logger
                logger.info(json.dumps({'timestamp': _now(), 'session': self.session,
                                        'event': event, 'pid': os.getpid(), **fields}, ensure_ascii=False))
        except Exception:
            pass

    def event(self, event, **fields):
        self._write('desktop_events.jsonl', event, **fields)

    def start(self):
        try:
            with self._lock:
                if self._started or self._closed:
                    return self
                self._started = True
                self.event('monitor_started', platform=sys.platform, interval_seconds=self._interval)
                if self._sampler is None:
                    if sys.platform != 'win32':
                        self.event('memory_sampling_unavailable', reason='windows_only')
                        return self
                    from backend.windows_memory import WindowsMemorySampler
                    self._sampler = WindowsMemorySampler()
                self._thread = threading.Thread(target=self._run, name='DesktopDiagnostics', daemon=True)
                self._thread.start()
                if self._watchdog_enabled and sys.platform == 'win32':
                    try:
                        from backend.desktop_watchdog import start_watchdog
                        self._watchdog = start_watchdog(self.directory, self.session)
                        self.event('watchdog_started', watchdog_pid=self._watchdog['process'].pid)
                    except Exception as exc:
                        self.event('watchdog_start_failed', error_type=type(exc).__name__)
        except Exception as exc:
            self.event('monitor_start_failed', error_type=type(exc).__name__)
        return self

    def _run(self):
        while not self._stop.is_set():
            self.sample()
            self._wake.wait(self._interval)
            self._wake.clear()

    def sample(self):
        try:
            snapshot = self._sampler()
            snapshot = {'captured_at': _now(), **snapshot}
            with self._lock:
                roles = dict(self._process_roles)
            for process in snapshot.get('processes', []):
                process['role'] = roles.get(process['pid'], 'Application' if process['pid'] == os.getpid() else 'Child')
            identities = {(p['pid'], p.get('created_filetime')) for p in snapshot.get('processes', [])}
            with self._lock:
                previous = self._previous_processes
                self._previous_processes = identities
                self._last_sample = snapshot
            self._write('desktop_memory.jsonl', 'memory_sample', **snapshot)
            if previous is not None and previous != identities:
                self.event('process_tree_changed', appeared=sorted(p[0] for p in identities - previous),
                           disappeared=sorted(p[0] for p in previous - identities))
        except Exception as exc:
            self.event('memory_sample_failed', error_type=type(exc).__name__)

    def attach_window(self, window):
        """pywebview callbacks run off the UI thread: marshal all COM access."""
        def shown():
            self.event('window_shown')
            self._queue_bind(window)

        def loaded():
            self.event('page_loaded')
            self._wake.set()
            self._queue_bind(window)

        try:
            self._windows.append(window)
            window.events.shown += shown
            window.events.loaded += loaded
        except Exception as exc:
            self.event('window_hook_failed', error_type=type(exc).__name__)

    @staticmethod
    def _dispatch(window, callback):
        from System import Action
        window.native.BeginInvoke(Action(callback))

    def _queue_bind(self, window):
        if self._stop.is_set() or sys.platform != 'win32':
            return
        try:
            self._dispatch(window, lambda: self._bind_native(window))
        except Exception as exc:
            self.event('webview_hook_failed', stage='dispatch', error_type=type(exc).__name__)

    def _subscribe(self, source, name, callback):
        hook = getattr(source, name)
        hook += callback
        self._bindings.append((source, name, callback))

    def _bind_native(self, window):
        if self._stop.is_set():
            return
        try:
            control = window.native.webview
            key = id(control)
            if key in self._bound_controls:
                return
            self._bound_controls.add(key)

            def ready(sender, args):
                try:
                    if args.IsSuccess:
                        self._bind_core(control.CoreWebView2)
                    else:
                        self.event('webview_initialization_failed')
                except Exception as exc:
                    self.event('webview_hook_failed', stage='initialization', error_type=type(exc).__name__)

            # Install initialization callback first, then check for an existing core.
            self._subscribe(control, 'CoreWebView2InitializationCompleted', ready)
            if control.CoreWebView2 is not None:
                self._bind_core(control.CoreWebView2)
        except Exception as exc:
            self.event('webview_hook_failed', stage='control', error_type=type(exc).__name__)

    def _bind_core(self, core):
        # Called only on the WinForms UI thread, including initialization callbacks.
        if any(source is core and name == 'ProcessFailed' for source, name, _ in self._bindings):
            return
        self._subscribe(core, 'ProcessFailed', self._process_failed)

        def navigation_starting(sender, args):
            try:
                self.event('navigation_starting', navigation_id=int(args.NavigationId))
            except Exception as exc:
                self.event('navigation_capture_failed', error_type=type(exc).__name__)

        def navigation_completed(sender, args):
            try:
                self.event('navigation_completed', navigation_id=int(args.NavigationId),
                           success=bool(args.IsSuccess), web_error_status=_enum(args.WebErrorStatus))
                self._wake.set()
            except Exception as exc:
                self.event('navigation_capture_failed', error_type=type(exc).__name__)

        self._subscribe(core, 'NavigationStarting', navigation_starting)
        self._subscribe(core, 'NavigationCompleted', navigation_completed)
        self.event('webview_hook_attached', browser_pid=int(core.BrowserProcessId))
        try:
            environment = core.Environment

            def browser_exited(sender, args):
                try:
                    self.event('browser_process_exited', browser_pid=int(args.BrowserProcessId),
                               exit_kind=_enum(args.BrowserProcessExitKind))
                    self._wake.set()
                except Exception as exc:
                    self.event('browser_exit_capture_failed', error_type=type(exc).__name__)

            self._subscribe(environment, 'BrowserProcessExited', browser_exited)
            version = str(environment.BrowserVersionString)
            if re.fullmatch(r'[0-9.]{1,64}', version):
                self.event('webview_runtime', version=version)
        except Exception as exc:
            self.event('webview_optional_hook_unavailable', error_type=type(exc).__name__)
        try:
            self._subscribe(core.Environment, 'ProcessInfosChanged', lambda sender, args: self._update_process_roles(sender))
            self._update_process_roles(core.Environment)
        except Exception as exc:
            self.event('webview_process_roles_unavailable', error_type=type(exc).__name__)

    def _update_process_roles(self, environment):
        try:
            roles = {int(info.ProcessId): _enum(info.Kind) for info in environment.GetProcessInfos()}
            with self._lock:
                self._process_roles = roles
            self.event('webview_processes', processes=[{'pid': pid, 'role': role} for pid, role in roles.items()])
            self._wake.set()
        except Exception as exc:
            self.event('webview_process_roles_unavailable', error_type=type(exc).__name__)

    def _process_failed(self, sender, args):
        # Do not read descriptions, frame URLs, user data paths or page contents.
        try:
            fields = {}
            unavailable = []
            for name, convert in (('ProcessFailedKind', _enum), ('Reason', _enum), ('ExitCode', int)):
                try:
                    fields[name] = convert(getattr(args, name))
                except Exception:
                    unavailable.append(name)
            with self._lock:
                fields['last_memory_sample'] = self._last_sample
            fields['unavailable_fields'] = unavailable
            self.event('webview_process_failed', **fields)
            self._wake.set()
        except Exception as exc:
            self.event('webview_failure_capture_failed', error_type=type(exc).__name__)

    def stop(self):
        try:
            from backend.desktop_watchdog import mark_normal_shutdown
            mark_normal_shutdown(self._watchdog)
        except Exception:
            pass
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread() and self._thread.is_alive():
            try:
                self._thread.join(timeout=1)
            except RuntimeError:
                pass
        with self._lock:
            if self._closed:
                return
            self.event('monitor_stopped')
            self._closed = True
            for logger in self._loggers.values():
                for handler in logger.handlers:
                    try:
                        handler.close()
                    except Exception:
                        pass


_diagnostics = None


def desktop_alert_status():
    """Report this process's helper health without starting any services."""
    if sys.platform != 'win32':
        return 'unsupported'
    if _diagnostics is None:
        return 'not_started'
    if _diagnostics._closed:
        return 'stopped'
    watchdog = _diagnostics._watchdog
    if not watchdog:
        return 'failed' if _diagnostics._watchdog_enabled else 'not_started'
    if watchdog['process'].poll() is not None:
        return 'stopped'
    try:
        from backend.desktop_alerts import read_json
        return 'running' if read_json(watchdog['state_file']).get('status') == 'watching' else 'starting'
    except (OSError, ValueError):
        return 'starting'


def start_desktop_diagnostics():
    global _diagnostics
    if _diagnostics is None:
        _diagnostics = DesktopDiagnostics(watchdog=True)
    return _diagnostics.start()


def stop_desktop_diagnostics():
    if _diagnostics is not None:
        _diagnostics.stop()
