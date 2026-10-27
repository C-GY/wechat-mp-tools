import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from backend.desktop_alerts import AlertPolicy, AlertSettings, DEFAULTS, deliver_alert
from backend.desktop_watchdog import JsonLogTail
from backend.diagnostics_export import diagnostics_bp


class DesktopAlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = AlertSettings(self.root)

    def bot(self, filename='pinchuang_hub_config.json', url='https://example.invalid/BOT_SENTINEL', secret='SECRET_SENTINEL'):
        data = {'database': {'host': 'test.aliyuncs.com', 'password': 'DB_SENTINEL'}, 'feishu': {'webhook_url': url, 'secret': secret}}
        (self.root / filename).write_text(json.dumps(data), encoding='utf-8')

    def test_reuses_same_configured_bot_once_and_keeps_all_credentials_private(self):
        self.bot()
        self.bot('guangce_hub_config.json')
        before = (self.root / 'pinchuang_hub_config.json').read_bytes()
        public = self.settings.save({'enabled': True})
        self.assertTrue(public['configured'])
        self.assertIn('历史通知配置 1', public['target_label'])
        self.assertEqual({item['source'] for item in public['legacy_sources']}, {'pinchuang', 'guangce'})
        self.assertNotIn('SENTINEL', json.dumps(public))
        self.assertNotIn('SENTINEL', self.settings.path.read_text(encoding='utf-8'))
        self.assertEqual(before, (self.root / 'pinchuang_hub_config.json').read_bytes())

    def test_different_bots_require_explicit_source_before_any_delivery(self):
        self.bot()
        self.bot('guangce_hub_config.json', url='https://example.invalid/OTHER_SENTINEL')
        self.assertFalse(self.settings.public()['configured'])
        with self.assertRaises(ValueError):
            self.settings.save({'enabled': True})
        self.assertTrue(self.settings.save({'source': 'guangce'})['configured'])

    def test_spikes_do_not_warn_sustained_pressure_warns_and_crash_bypasses_warning_cooldown(self):
        clock = [0]
        sent = []
        policy = AlertPolicy(10, lambda *args: sent.append(args) or {'sent': True}, clock=lambda: clock[0])
        high = {'event': 'memory_sample', 'processes': [{'pid': 11, 'role': 'Renderer', 'created_filetime': 1, 'private_bytes': 1600 * 1024**2}]}
        low = {'event': 'memory_sample', 'processes': []}
        policy.consume(high, DEFAULTS)
        clock[0] = 30
        policy.consume(low, DEFAULTS)
        clock[0] = 45
        policy.consume(high, DEFAULTS)
        clock[0] = 90
        policy.consume(high, DEFAULTS)
        self.assertEqual(sent, [])
        clock[0] = 105
        policy.consume(high, DEFAULTS)
        self.assertEqual(len(sent), 1)
        self.assertIn('风险预警', sent[0][0])
        policy.consume(high, DEFAULTS)
        self.assertEqual(len(sent), 1)
        failure = {'event': 'webview_process_failed', 'ProcessFailedKind': 'RenderProcessExited', 'Reason': 'OutOfMemory', 'ExitCode': 1}
        policy.consume(failure, DEFAULTS)
        policy.consume(failure, DEFAULTS)
        self.assertEqual(len(sent), 2)
        self.assertIn('OutOfMemory', sent[-1][1])
        policy.exited(23, False, DEFAULTS)
        self.assertEqual(len(sent), 3)
        self.assertIn('23', sent[-1][1])

    def test_normal_shutdown_disabled_alerts_and_auto_recoverable_gpu_exit_do_not_notify(self):
        send = Mock()
        policy = AlertPolicy(10, send)
        policy.exited(0, True, DEFAULTS)
        policy.exited(1, False, {**DEFAULTS, 'enabled': False})
        policy.consume({'event': 'webview_process_failed', 'ProcessFailedKind': 'GpuProcessExited'}, DEFAULTS)
        policy.consume({'event': 'browser_process_exited', 'exit_kind': 'Normal'}, DEFAULTS)
        send.assert_not_called()

    def test_renderer_replacement_resets_sustained_memory_timer(self):
        clock = [0]
        send = Mock(return_value={'sent': True})
        policy = AlertPolicy(10, send, clock=lambda: clock[0])
        sample = {'event': 'memory_sample', 'processes': [{'pid': 11, 'created_filetime': 1, 'role': 'Renderer', 'private_bytes': 2 * 1024**3}]}
        policy.consume(sample, DEFAULTS)
        clock[0] = 50
        sample['processes'][0]['created_filetime'] = 2
        policy.consume(sample, DEFAULTS)
        clock[0] = 70
        policy.consume(sample, DEFAULTS)
        send.assert_not_called()

    def test_system_commit_and_main_process_memory_warn_after_sustained_limit(self):
        clock, sent = [0], []
        policy = AlertPolicy(10, lambda *args: sent.append(args) or {'sent': True}, clock=lambda: clock[0])
        high = {'event': 'memory_sample', 'system': {'committed_bytes': 95, 'commit_limit_bytes': 100},
                'processes': [{'pid': 10, 'private_bytes': 3 * 1024**3}]}
        policy.consume(high, DEFAULTS)
        clock[0] = 60
        policy.consume(high, DEFAULTS)
        self.assertEqual(len(sent), 1)
        self.assertIn('95.0%', sent[0][1])
        self.assertIn('主程序', sent[0][1])

    def test_failed_delivery_has_short_retry_cooldown_and_unsafe_reason_is_not_forwarded(self):
        clock, sent = [0], []
        policy = AlertPolicy(10, lambda *args: sent.append(args) or {'sent': False}, clock=lambda: clock[0])
        record = {'event': 'webview_process_failed', 'ProcessFailedKind': 'RenderProcessUnresponsive', 'Reason': 'SECRET SENTINEL'}
        policy.consume(record, DEFAULTS)
        clock[0] = 59
        policy.consume(record, DEFAULTS)
        self.assertEqual(len(sent), 1)
        clock[0] = 60
        policy.consume(record, DEFAULTS)
        self.assertEqual(len(sent), 2)
        self.assertNotIn('SENTINEL', str(sent))

    def test_log_tail_handles_partial_lines_and_rotation_without_duplicate_records(self):
        path = self.root / 'events.jsonl'
        path.write_bytes(b'{"event":')
        tail = JsonLogTail(path)
        self.assertEqual(tail.read(), [])
        with path.open('ab') as output:
            output.write(b'"first"}\n')
        self.assertEqual(tail.read(), [{'event': 'first'}])
        self.assertEqual(tail.read(), [])
        path.rename(path.with_suffix('.old'))
        path.write_text('{"event":"second"}\n', encoding='utf-8')
        self.assertEqual(tail.read(), [{'event': 'second'}])

    def test_delivery_does_not_use_dead_app_proxy_and_never_returns_credentials(self):
        self.bot()
        from unittest.mock import MagicMock
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.return_value.json.return_value = {'code': 0}
        with patch('requests.Session', return_value=http):
            result = deliver_alert(self.settings, DEFAULTS, 'test', 'safe details', 'session', 10)
        self.assertTrue(result['sent'])
        self.assertFalse(http.trust_env)
        payload = http.post.call_args.kwargs['json']
        self.assertIn('sign', payload)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(payload))
        self.assertNotIn('SENTINEL', json.dumps(result))
        http.post.side_effect = RuntimeError('SECRET_SENTINEL')
        with patch('requests.Session', return_value=http), patch('backend.feishu_notifier.time.sleep'):
            failed = deliver_alert(self.settings, DEFAULTS, 'test', 'safe', 'session', 10)
        self.assertFalse(failed['sent'])
        self.assertEqual(failed['attempts'], 3)
        self.assertNotIn('SENTINEL', json.dumps(failed))

    def test_alert_api_masks_config_and_tests_the_selected_unsaved_source_without_overwriting_settings(self):
        self.bot()
        self.bot('guangce_hub_config.json', url='https://example.invalid/OTHER_SENTINEL')
        app = Flask(__name__)
        app.register_blueprint(diagnostics_bp)
        with patch('backend.desktop_alerts.AlertSettings', return_value=self.settings):
            client = app.test_client()
            response = client.get('/api/diagnostics/alerts')
            self.assertFalse(response.json['configured'])
            self.assertNotIn('SENTINEL', response.get_data(as_text=True))
            self.assertEqual(client.post('/api/diagnostics/alerts', json={'source': 'pinchuang'}).status_code, 200)
            original = self.settings.path.read_bytes()
            with patch('backend.desktop_alerts.deliver_alert', return_value={'sent': True}) as send:
                response = client.post('/api/diagnostics/alerts/test', json={'source': 'guangce'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(send.call_args.args[1]['source'], 'guangce')
            self.assertEqual(self.settings.path.read_bytes(), original)
            self.assertEqual(client.post('/api/diagnostics/alerts', json={'sustain_seconds': 0}).status_code, 400)

    def test_watchdog_entry_exits_before_gui_configuration_or_business_startup(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, 'argv', ['main.py', '--desktop-watchdog', 'state', '123']), \
             patch('backend.desktop_watchdog.watchdog_main', return_value=0) as run, \
             patch('backend.runtime.configure_runtime') as configure, \
             patch('backend.desktop_diagnostics.start_desktop_diagnostics') as start, \
             patch('multiprocessing.freeze_support'):
            with self.assertRaises(SystemExit) as outcome:
                runpy.run_path(str(root / 'main.py'), run_name='__main__')
        self.assertEqual(outcome.exception.code, 0)
        run.assert_called_once_with(['state', '123'])
        configure.assert_not_called()
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
