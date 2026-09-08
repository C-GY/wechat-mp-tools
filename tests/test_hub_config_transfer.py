import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from backend import competitor_monitor, creative_radar, guangce, pinchuang


class HubConfigFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (pinchuang, creative_radar):
            self.stack.enter_context(patch.object(module, "get_oss_config", return_value={"configured": False}))
        self.hubs = {
            "pinchuang": pinchuang.PinchuangHub(self.root / "pinchuang.json", self.root / "pinchuang-state.json"),
            "creative-radar": creative_radar.CreativeRadarHub(self.root / "radar.json", self.root / "radar-state.json"),
            "guangce": guangce.GuangceHub(self.root / "guangce.json", self.root / "guangce-state.json"),
            "competitor_monitor": competitor_monitor.CompetitorMonitorHub(self.root / "competitor.json", self.root / "competitor-state.json"),
        }
        common = {
            "schedule": {"enabled": True, "times": ["09:00", "18:30"], "creator_interval_seconds": 25},
            "feishu": {"webhook_url": "https://example.invalid/bot", "secret": "test-bot-secret"},
        }
        self.hubs["pinchuang"].save_config({
            **common,
            "database": {"host": "db.example.invalid", "port": 3307, "username": "writer",
                         "password": "test-db-secret", "database": "pinchuang_platform"},
        })
        self.hubs["creative-radar"].save_config({
            **common,
            "api": {"endpoint": "https://example.invalid/upload", "api_key": "test-api-secret", "timeout_seconds": 90},
        })
        self.hubs["guangce"].save_config({
            **common,
            "database": {"host": "guangce.example.invalid", "port": 3306, "username": "guangce-writer",
                         "password": "test-db-secret", "database": "guangce_platform"},
        })
        self.hubs["competitor_monitor"].save_config({
            **common,
            "database": {"host": "competitor.example.invalid", "port": 3306, "username": "competitor-writer",
                         "password": "test-db-secret", "database": "competitor_monitor"},
        })
        self.stack.enter_context(patch.object(pinchuang, "pinchuang_hub", self.hubs["pinchuang"]))
        self.stack.enter_context(patch.object(creative_radar, "creative_radar_hub", self.hubs["creative-radar"]))
        self.stack.enter_context(patch.object(guangce, "guangce_hub", self.hubs["guangce"]))
        self.stack.enter_context(patch.object(competitor_monitor, "competitor_monitor_hub", self.hubs["competitor_monitor"]))
        self.app = Flask(__name__)
        self.app.register_blueprint(pinchuang.pinchuang_bp)
        self.app.register_blueprint(creative_radar.creative_radar_bp)
        self.app.register_blueprint(guangce.guangce_bp)
        self.app.register_blueprint(competitor_monitor.competitor_monitor_bp)
        self.client = self.app.test_client()


class HubConfigTransferTests(HubConfigFixture):
    def test_export_includes_saved_secrets_and_normal_reads_remain_masked(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                before = hub.config_path.read_bytes()
                exported = self.client.post(f"/api/{key}/config/export")
                self.assertEqual(exported.status_code, 200)
                self.assertEqual(exported.headers["Cache-Control"], "no-store")
                backup = exported.get_json()
                self.assertEqual(backup["config"], hub.config)
                self.assertEqual(backup["format_version"], 1)
                self.assertEqual(backup["format"], hub.config_backup_format)
                self.assertTrue(backup["exported_at"])
                self.assertEqual(hub.config_path.read_bytes(), before)
                public = self.client.get(f"/api/{key}/config").get_data(as_text=True)
                for secret in ("test-db-secret", "test-api-secret", "test-bot-secret", "https://example.invalid/bot"):
                    self.assertNotIn(secret, public)

    def test_import_restores_full_config_persists_and_keeps_other_module_and_history(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                backup = self.client.post(f"/api/{key}/config/export").get_json()
                expected = copy.deepcopy(hub.config)
                others_before = {other.config_path: other.config_path.read_bytes()
                                 for other_key, other in self.hubs.items() if other_key != key}
                state_before = hub.state_path.read_bytes()
                hub.save_config({"schedule": {"enabled": False, "times": ["12:00"]}})
                response = self.client.post(f"/api/{key}/config/import", json=backup)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(hub.config, expected)
                self.assertEqual(json.loads(hub.config_path.read_text(encoding="utf-8")), expected)
                restarted = type(hub)(hub.config_path, self.root / f"{key}-restarted.json")
                self.assertEqual(restarted.config, expected)
                for path, before in others_before.items():
                    self.assertEqual(path.read_bytes(), before)
                self.assertEqual(hub.state_path.read_bytes(), state_before)
                self.assertIsNone(hub.worker)
                for secret in ("test-db-secret", "test-api-secret", "test-bot-secret"):
                    self.assertNotIn(secret, response.get_data(as_text=True))

    def test_import_blank_secrets_clears_old_credentials(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                empty = hub._merge_config({})
                response = self.client.post(f"/api/{key}/config/import", json=empty)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(hub.config, empty)
                self.assertFalse(hub.config["feishu"]["secret"])
                self.assertFalse(hub.config["feishu"]["webhook_url"])

    def test_bad_json_wrong_module_and_invalid_fields_do_not_change_config(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                backup = hub.export_config_backup()
                bad_enabled = copy.deepcopy(backup)
                bad_enabled["config"]["schedule"]["enabled"] = "false"
                bad_times = copy.deepcopy(backup)
                bad_times["config"]["schedule"]["times"] = ["25:00"]
                bad_field = copy.deepcopy(backup)
                connection = "database" if "database" in hub.config else "api"
                number = "port" if connection == "database" else "timeout_seconds"
                bad_field["config"][connection][number] = -1
                incomplete = copy.deepcopy(backup)
                del incomplete["config"][connection]["password" if connection == "database" else "api_key"]
                other = next(other for other in self.hubs.values() if set(other.config) != set(hub.config))
                payloads = [
                    None, [], {}, {"format": hub.config_backup_format},
                    {**backup, "format_version": 2}, {**backup, "format_version": True},
                    *(other.export_config_backup() for other_key, other in self.hubs.items() if other_key != key),
                    other.config, incomplete,
                    bad_enabled, bad_times, bad_field, hub.get_config(),
                ]
                before = hub.config_path.read_bytes()
                current = copy.deepcopy(hub.config)
                for payload in payloads:
                    response = self.client.post(
                        f"/api/{key}/config/import", data=json.dumps(payload), content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertTrue(response.get_json()["error"])
                    self.assertEqual(hub.config_path.read_bytes(), before)
                    self.assertEqual(hub.config, current)
                response = self.client.post(f"/api/{key}/config/import", data="{invalid", content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(hub.config_path.read_bytes(), before)

    def test_write_failure_keeps_config_on_disk_and_in_memory(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                before = hub.config_path.read_bytes()
                current = copy.deepcopy(hub.config)
                incoming = hub._merge_config({})
                with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                    response = self.client.post(f"/api/{key}/config/import", json=incoming)
                self.assertEqual(response.status_code, 500)
                self.assertIn("原配置未更改", response.get_json()["error"])
                self.assertEqual(hub.config_path.read_bytes(), before)
                self.assertEqual(hub.config, current)


if __name__ == "__main__":
    unittest.main()
