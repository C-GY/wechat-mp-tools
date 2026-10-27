"""Hidden, same-app watchdog. No Flask, hubs, schedulers or database imports."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from backend.desktop_alerts import AlertPolicy, AlertSettings, atomic_json, deliver_alert, read_json


class ParentProcess:
    def __init__(self, pid=None, handle=None):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        specs = (
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('GetProcessId', [wintypes.HANDLE], wintypes.DWORD),
            ('GetProcessTimes', [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('GetExitCodeProcess', [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
        )
        for name, args, result in specs:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        # SYNCHRONIZE | QUERY_LIMITED_INFORMATION, explicitly inherited by this child only.
        self.handle = handle if handle is not None else self.kernel.OpenProcess(0x100000 | 0x1000, True, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def identity(self):
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        return self.kernel.GetProcessId(self.handle), (created.dwHighDateTime << 32) | created.dwLowDateTime

    def exited(self, timeout_ms=1000):
        result = self.kernel.WaitForSingleObject(self.handle, timeout_ms)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def exit_code(self):
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def close(self):
        self.kernel.CloseHandle(self.handle)


class JsonLogTail:
    def __init__(self, path):
        self.path, self.identity, self.offset, self.pending = Path(path), None, 0, b''
        self.more = False

    def read(self):
        self.more = False
        try:
            with self.path.open('rb') as source:
                info = os.fstat(source.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self.identity or info.st_size < self.offset:
                    self.identity, self.offset, self.pending = identity, 0, b''
                source.seek(self.offset)
                chunk = source.read(2 * 1024 * 1024)
                self.offset += len(chunk)
                self.more = self.offset < info.st_size
        except OSError:
            return []
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        if len(self.pending) > 256 * 1024:
            self.pending = b''
        records = []
        for line in lines:
            if len(line) > 256 * 1024:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, UnicodeError):
                pass
        return records


def start_watchdog(directory, session):
    if sys.platform != 'win32':
        return None
    path = Path(directory) / f'desktop-watchdog-{session}.json'
    completed = []
    for candidate in path.parent.glob('desktop-watchdog-*.json'):
        if re.fullmatch(r'desktop-watchdog-[0-9a-f]{32}\.json', candidate.name):
            try:
                if read_json(candidate).get('status') in ('normal_exit', 'unexpected_exit', 'normal_shutdown_requested', 'start_failed'):
                    completed.append((candidate.stat().st_mtime, candidate))
            except (OSError, ValueError):
                pass
    for _, candidate in sorted(completed, key=lambda item: item[0], reverse=True)[19:]:
        try:
            candidate.unlink(missing_ok=True)
            candidate.with_suffix('.stop').unlink(missing_ok=True)
        except OSError:
            pass
    parent = ParentProcess(pid=os.getpid())
    try:
        pid, created = parent.identity()
        atomic_json(path, dict(session=session, parent_pid=pid, parent_created=created, status='starting'))
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        info.lpAttributeList = {'handle_list': [int(parent.handle)]}
        command = [sys.executable]
        if not getattr(sys, 'frozen', False):
            command.append(str(Path(__file__).resolve().parents[1] / 'main.py'))
        command.extend(['--desktop-watchdog', str(path), str(int(parent.handle))])
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, startupinfo=info, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        return {'process': process, 'state_file': path}
    except Exception:
        atomic_json(path, dict(session=session, status='start_failed'))
        raise
    finally:
        parent.close()


def mark_normal_shutdown(watchdog):
    if watchdog:
        watchdog['state_file'].with_suffix('.stop').touch()


def run_watchdog(state_path, parent, settings=None, send=None):
    path = Path(state_path)
    if not re.fullmatch(r'desktop-watchdog-[0-9a-f]{32}\.json', path.name):
        raise ValueError('Invalid watchdog state file')
    state = read_json(path)
    if parent.identity() != (state['parent_pid'], state['parent_created']) or path.stem != 'desktop-watchdog-' + state['session']:
        raise ValueError('Parent identity mismatch')
    settings = settings or AlertSettings()
    tails = [JsonLogTail(path.parent / name) for name in ('desktop_memory.jsonl', 'desktop_events.jsonl')]
    state.update(status='watching', watchdog_pid=os.getpid())
    def checkpoint():
        try:
            atomic_json(path, state)
        except OSError:
            pass  # Full/unwritable disk must not prevent sending an alert.
    checkpoint()

    def notify(title, details, config):
        try:
            result = (send or deliver_alert)(settings, config, title, details, state['session'], state['parent_pid'])
        except Exception as exc:
            result = {'sent': False, 'error_type': type(exc).__name__}
        # Keep credentials, URLs, server replies and arbitrary exception messages out of logs.
        state['last_notification'] = {key: result[key] for key in ('sent', 'attempts', 'skipped', 'reason', 'error_type') if key in result}
        state['last_notification']['time'] = time.time()
        state['last_notification']['title'] = title
        checkpoint()
        return result

    policy = AlertPolicy(state['parent_pid'], notify)
    normal = False
    while True:
        try:
            config = settings.load()
        except Exception as exc:
            state['configuration_error_type'] = type(exc).__name__
            checkpoint()
            config = dict(enabled=False)
        # Read all chunks on exit too, so a last renderer failure is not discarded.
        parent_exited = parent.exited(1000)
        for tail in tails:
            while True:
                records = tail.read()
                for record in records:
                    if record.get('session') != state['session']:
                        continue
                    if record.get('event') == 'monitor_stopped':
                        normal = True
                    try:
                        policy.consume(record, config)
                    except (ValueError, TypeError, KeyError):
                        pass
                if not parent_exited or not tail.more:
                    break
        normal = normal or path.with_suffix('.stop').exists()
        if parent_exited:
            code = parent.exit_code()
            policy.exited(code, normal, config)
            state.update(status='normal_exit' if normal else 'unexpected_exit', exit_code=code)
            checkpoint()
            return 0
        if normal:
            state['status'] = 'normal_shutdown_requested'
            checkpoint()
            return 0


def watchdog_main(arguments):
    if len(arguments) != 2 or sys.platform != 'win32':
        return 2
    parent = None
    try:
        parent = ParentProcess(handle=int(arguments[1]))
        return run_watchdog(arguments[0], parent)
    except Exception:
        return 1
    finally:
        if parent:
            parent.close()
