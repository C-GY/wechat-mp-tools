"""Real parent-process death -> hidden watchdog -> local Feishu-shaped receiver."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


@unittest.skipUnless(sys.platform == 'win32', 'Windows inherited process handles')
class WatchdogIntegrationTests(unittest.TestCase):
    def exercise(self, normal):
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"code":0}')

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / 'profile' / 'Fandow' / 'SelfMediaContentCollector'
                config.mkdir(parents=True)
                (config / 'pinchuang_hub_config.json').write_text(json.dumps({'feishu': {'webhook_url': f'http://127.0.0.1:{server.server_port}/hook'}}), encoding='utf-8')
                session = uuid.uuid4().hex
                logs = root / 'logs'
                code = '''
from pathlib import Path
import os,sys
from backend.desktop_watchdog import start_watchdog, mark_normal_shutdown
watchdog = start_watchdog(Path(sys.argv[1]), sys.argv[2])
if sys.argv[3] == 'normal':
    mark_normal_shutdown(watchdog)
os._exit(0 if sys.argv[3] == 'normal' else 23)
'''
                parent = subprocess.Popen([sys.executable, '-c', code, str(logs), session, 'normal' if normal else 'crash'],
                    cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ, 'APPDATA': str(root / 'profile')},
                    creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, stderr = parent.communicate(timeout=10)
                self.assertEqual(parent.returncode, 0 if normal else 23, stderr.decode(errors='replace'))
                state_path = logs / f'desktop-watchdog-{session}.json'
                deadline = time.monotonic() + 20
                state = {}
                while time.monotonic() < deadline:
                    if state_path.exists():
                        state = json.loads(state_path.read_text(encoding='utf-8'))
                        if state.get('status') in ('normal_exit', 'unexpected_exit', 'normal_shutdown_requested'):
                            break
                    time.sleep(0.1)
                self.assertIn(state.get('status'), ('normal_exit', 'unexpected_exit', 'normal_shutdown_requested'), state)
                if normal:
                    self.assertEqual(received, [])
                else:
                    self.assertEqual(state['exit_code'], 23)
                    self.assertTrue(state['last_notification']['sent'])
                    self.assertEqual(len(received), 1)
                    self.assertIn('异常退出', received[0]['card']['header']['title']['content'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_abrupt_parent_exit_notifies_after_parent_is_gone(self):
        self.exercise(False)

    def test_normal_shutdown_does_not_send_an_exit_alert(self):
        self.exercise(True)


if __name__ == '__main__':
    unittest.main()
