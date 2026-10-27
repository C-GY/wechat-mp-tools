import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from flask import Flask
from backend.diagnostics_export import create_diagnostics_archive, diagnostics_bp


class DiagnosticsExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data' / 'logs').mkdir(parents=True)

    def test_export_contains_rotated_memory_and_errors_but_never_data_or_credentials(self):
        for name in ('desktop_memory.jsonl', 'desktop_memory.jsonl.3', 'desktop_events.jsonl'):
            (self.root / 'data' / 'logs' / name).write_text('{"event":"OutOfMemory"}\n', encoding='utf-8')
        (self.root / 'data' / 'frontend_errors.log').write_bytes(b'Error\r\nCookie: COOKIE_SENTINEL\r\npassword=PASSWORD_SENTINEL\r\nhttps://example.com/?token=URL_SENTINEL\r\n')
        (self.root / 'data' / 'wechat_mp_config.json').write_text('CONFIG_SENTINEL', encoding='utf-8')
        (self.root / 'data' / 'channels_data.sqlite3').write_text('DATABASE_SENTINEL', encoding='utf-8')
        receipt = 'desktop-watchdog-' + 'a' * 32 + '.json'
        (self.root / 'data' / 'logs' / receipt).write_text('{"status":"unexpected_exit","last_notification":{"sent":true}}', encoding='utf-8')
        source = self.root / 'data' / 'logs' / 'desktop_events.jsonl'
        before = source.read_bytes()
        path = create_diagnostics_archive(self.root)
        self.assertEqual(source.read_bytes(), before)
        with ZipFile(path) as archive:
            text = '\n'.join(archive.read(n).decode('utf-8') for n in archive.namelist())
            self.assertNotIn('SENTINEL', text)
            self.assertIn('OutOfMemory', text)
            self.assertIn('watchdog/' + receipt, archive.namelist())
            self.assertTrue(any(n.endswith('desktop_memory.jsonl.3.txt') for n in archive.namelist()))
            self.assertFalse(any('config.json' in n or 'sqlite3' in n for n in archive.namelist()))
            inventory = json.loads(archive.read('inventory.json'))
            self.assertEqual(sum(i['status'] == 'collected' for i in inventory['files']), 4)

    def test_large_single_line_is_bounded_and_not_partially_exposed(self):
        (self.root / 'wechat_mp_tools.log').write_bytes(b'SECRET_SENTINEL' + b'X' * (3 * 1024 * 1024))
        with ZipFile(create_diagnostics_archive(self.root)) as archive:
            text = archive.read('logs/wechat_mp_tools.log.txt').decode('utf-8')
            self.assertIn('Oversized single line omitted', text)
            self.assertLess(len(text), 200)
            self.assertNotIn('SENTINEL', text)

    def test_export_endpoint_uses_fixed_location_and_handles_failure(self):
        app = Flask(__name__)
        app.register_blueprint(diagnostics_bp)
        client = app.test_client()
        with patch('backend.diagnostics_export.app_dir', return_value=self.root):
            self.assertEqual(client.get('/api/diagnostics/export').status_code, 405)
            response = client.post('/api/diagnostics/export', json={'path': 'UNTRUSTED_PATH'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['has_desktop_diagnostics'])
        path = Path(response.json['path'])
        self.assertTrue(path.is_relative_to(self.root))
        self.assertTrue(path.is_file())
        with patch('backend.diagnostics_export.create_diagnostics_archive', side_effect=PermissionError('SECRET_SENTINEL')):
            failed = client.post('/api/diagnostics/export')
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn('SENTINEL', failed.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
