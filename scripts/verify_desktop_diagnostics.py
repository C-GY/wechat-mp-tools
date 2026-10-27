"""Verify real WebView2 crash/reload capture in an isolated, hidden test window.

Does not import app.py, access business configuration or connect to databases.
Run on Windows with the project's Python: python scripts/verify_desktop_diagnostics.py
"""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if sys.platform != 'win32':
        raise SystemExit('This verification requires Windows and WebView2.')
    import webview
    from backend.desktop_diagnostics import DesktopDiagnostics

    output = ROOT / 'scratch' / 'diagnostics' / ('desktop-native-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    diagnostics = DesktopDiagnostics(output, interval=0.2).start()
    window = webview.create_window('Isolated diagnostic verification', html='<h1>Diagnostic verification</h1>', hidden=True)
    diagnostics.attach_window(window)
    errors = []

    def records():
        try:
            return [json.loads(line) for line in (output / 'desktop_events.jsonl').read_text(encoding='utf-8').splitlines()]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def wait_for(predicate):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = records()
            if predicate(events):
                return events
            time.sleep(0.05)
        raise RuntimeError('Timed out waiting for native diagnostic event')

    def exercise():
        try:
            # Otherwise pywebview's initial HTML navigation can replace chrome://crash.
            wait_for(lambda events: any(e['event'] == 'webview_hook_attached' for e in events)
                     and any(e['event'] == 'page_loaded' for e in events))
            # Intentionally crash only this new, isolated window's renderer.
            diagnostics._dispatch(window, lambda: window.native.webview.CoreWebView2.Navigate('chrome://crash'))
            wait_for(lambda events: any(e['event'] == 'webview_process_failed' for e in events))
            failure_index = next(i for i, e in enumerate(records()) if e['event'] == 'webview_process_failed')
            diagnostics._dispatch(window, lambda: window.native.webview.CoreWebView2.NavigateToString('<h1>Recovered</h1>'))
            wait_for(lambda events: any(e['event'] == 'navigation_completed' and e.get('success') for e in events[failure_index + 1:]))
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            window.destroy()

    try:
        webview.start(exercise, private_mode=True, storage_path=str(output / 'isolated-webview-profile'))
    finally:
        diagnostics.stop()
    events = records()
    failures = [e for e in events if e['event'] == 'webview_process_failed']
    if errors or not failures:
        print(json.dumps({'errors': errors, 'events': [e['event'] for e in events]}))
        raise SystemExit(1)
    failure = failures[0]
    assert failure['ProcessFailedKind'] == 'RenderProcessExited', failure
    assert failure['last_memory_sample']['processes'], 'Missing pre-failure native process data'
    print(json.dumps({'result': 'PASS', 'failure_kind': failure['ProcessFailedKind'],
                      'reason': failure.get('Reason'), 'recovery': 'navigation_completed', 'logs': str(output)}))


if __name__ == '__main__':
    main()
