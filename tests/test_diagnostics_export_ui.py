"""Real settings-page click -> isolated Flask blueprint -> diagnostic ZIP."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from flask import Flask
from playwright.sync_api import sync_playwright

from backend.diagnostics_export import diagnostics_bp
from backend.desktop_alerts import AlertSettings


class DiagnosticsSettingsTests(unittest.TestCase):
    def test_existing_bot_selection_save_test_and_validation_work_in_real_page(self):
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__)
        app.register_blueprint(diagnostics_bp)
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            settings = AlertSettings(tmp)
            for module in ('pinchuang', 'guangce'):
                (Path(tmp) / f'{module}_hub_config.json').write_text(json.dumps({
                    'feishu': {'webhook_url': f'https://example.invalid/{module}_SENTINEL', 'secret': 'SECRET_SENTINEL'}
                }), encoding='utf-8')
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                errors, sent = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def deliver(*args):
                    sent.append(args[1]['source'])
                    return {'sent': True}

                def handle(route):
                    request = route.request
                    endpoint = request.url.removeprefix('https://diagnostics.test')
                    if endpoint.startswith('/api/diagnostics/'):
                        with patch('backend.desktop_alerts.AlertSettings', return_value=settings), \
                             patch('backend.desktop_alerts.deliver_alert', side_effect=deliver), \
                             patch('backend.desktop_diagnostics.desktop_alert_status', return_value='running'):
                            response = app.test_client().open(endpoint, method=request.method,
                                                              data=request.post_data, content_type='application/json')
                        route.fulfill(status=response.status_code, content_type='application/json', body=response.data)
                    else:
                        route.fulfill(content_type='text/html', body='<meta charset="utf-8"><main id="app"></main>')

                page.route('https://diagnostics.test/**', handle)
                page.goto('https://diagnostics.test/')
                page.add_script_tag(path=str(root / 'frontend/js/api.js'))
                page.add_script_tag(path=str(root / 'frontend/js/components/settings.js'))
                page.evaluate("""async () => {
                    window.__messages = [];
                    window.Toast = {success: text => __messages.push(text), error: text => __messages.push(text)};
                    document.getElementById('app').innerHTML = SettingsPage.render();
                    await SettingsPage.loadDesktopAlerts();
                }""")
                self.assertIn('多个不同机器人', page.locator('#desktop-alert-target').inner_text())
                self.assertIn('运行中', page.locator('#desktop-alert-watchdog').inner_text())
                page.locator('#desktop-alert-source').select_option('pinchuang')
                page.get_by_role('button', name='保存告警设置', exact=True).click()
                page.wait_for_function("document.getElementById('desktop-alert-target').textContent.includes('历史通知配置 1')")
                self.assertEqual(settings.load()['source'], 'pinchuang')
                page.locator('#desktop-alert-source').select_option('guangce')
                page.get_by_role('button', name='发送告警测试', exact=True).click()
                page.wait_for_function("__messages.includes('告警测试消息已发送')")
                self.assertEqual(sent, ['guangce'])
                self.assertEqual(settings.load()['source'], 'pinchuang')
                page.locator('#desktop-alert-sustain').fill('0')
                page.get_by_role('button', name='保存告警设置', exact=True).click()
                page.wait_for_function("__messages.some(text => text.includes('sustain_seconds'))")
                self.assertEqual(settings.load()['sustain_seconds'], 60)
                self.assertTrue(page.locator('#desktop-alert-save').is_enabled())
                self.assertTrue(page.locator('#desktop-alert-test').is_enabled())
                self.assertNotIn('SENTINEL', page.locator('body').inner_text())
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_export_button_creates_archive_shows_path_and_recovers_after_error(self):
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__)
        app.register_blueprint(diagnostics_bp)
        requests = []
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            directory = Path(tmp)
            (directory / 'data' / 'logs').mkdir(parents=True)
            (directory / 'data' / 'logs' / 'desktop_events.jsonl').write_text('{"event":"webview_process_failed","Reason":"OutOfMemory"}\n', encoding='utf-8')
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 950})
                page_errors = []
                page.on('pageerror', lambda error: page_errors.append(str(error)))

                def handle(route):
                    request = route.request
                    if request.url.endswith('/api/diagnostics/export'):
                        requests.append(request.method)
                        with patch('backend.diagnostics_export.app_dir', return_value=directory):
                            response = app.test_client().open('/api/diagnostics/export', method=request.method)
                        route.fulfill(status=response.status_code, content_type='application/json', body=response.data)
                    else:
                        route.fulfill(content_type='text/html', body='<meta charset="utf-8"><main id="app"></main>')

                page.route('https://diagnostics.test/**', handle)
                page.goto('https://diagnostics.test/')
                page.add_style_tag(path=str(root / 'frontend/css/style.css'))
                page.add_script_tag(path=str(root / 'frontend/js/api.js'))
                page.add_script_tag(path=str(root / 'frontend/js/components/settings.js'))
                page.evaluate("""() => {
                    window.__messages = [];
                    window.Toast = {success: text => __messages.push(text), error: text => __messages.push(text)};
                    document.getElementById('app').innerHTML = SettingsPage.render();
                }""")
                button = page.get_by_role('button', name='导出诊断日志', exact=True)
                button.click()
                page.wait_for_function("document.getElementById('diagnostics-export-result').textContent.includes('.zip')")
                self.assertTrue(button.is_enabled())
                self.assertEqual(requests, ['POST'])
                archive_path = next((directory / 'data' / 'diagnostics').glob('*.zip'))
                self.assertIn(str(archive_path), page.get_by_role('status').inner_text())
                with ZipFile(archive_path) as archive:
                    self.assertIn('OutOfMemory', archive.read('logs/data/logs/desktop_events.jsonl.txt').decode('utf-8'))

                page.route('https://diagnostics.test/api/diagnostics/export', lambda route: route.fulfill(
                    status=500, content_type='application/json', body=json.dumps({'error': 'test failure'})))
                button.click()
                page.wait_for_function("document.getElementById('diagnostics-export-result').textContent.includes('导出失败')")
                self.assertTrue(button.is_enabled())
                self.assertEqual(page_errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
