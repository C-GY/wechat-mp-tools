import json
from datetime import datetime
from unittest.mock import MagicMock, patch

from backend import guangce, pinchuang
from test_hub_config_transfer import HubConfigFixture
import test_pinchuang as pinchuang_tests


class GuangcePipelineTests(pinchuang_tests.PipelineTests):
    hub_class = guangce.GuangceHub


class GuangcePauseControlTests(pinchuang_tests.PauseControlTests):
    hub_class = guangce.GuangceHub


class GuangceHubTests(HubConfigFixture):
    def test_defaults_and_database_connections_belong_to_guangce(self):
        hub = guangce.GuangceHub(self.root / "new.json", self.root / "new-state.json")
        config = hub.get_config()
        self.assertEqual(config["database"]["database"], "")
        self.assertFalse(config["configured"])
        self.assertFalse(config["schedule"]["enabled"])
        self.assertEqual(config["table"], "competitor_video_snapshots")
        self.assertEqual(config["storage_path"], str(guangce.CONFIG_FILE))
        self.assertNotEqual(guangce.CONFIG_FILE, pinchuang.PINCHUANG_CONFIG_FILE)
        self.assertNotEqual(guangce.STATE_FILE, pinchuang.PINCHUANG_STATE_FILE)
        for database in ("", "  ", None):
            self.assertEqual(hub.save_config({"database": {"database": database}})["database"]["database"], "")
        self.assertEqual(hub.save_config({"schedule": {"times": ["10:00"]}})["database"]["database"], "")
        hub.save_config({"database": {"database": "custom_guangce"}})
        hub.save_config({"schedule": {"times": ["10:00"]}})
        self.assertEqual(hub._database_adapter().config["database"], "custom_guangce")
        self.assertEqual(pinchuang.DEFAULT_CONFIG["database"]["database"], "pinchuang_platform")

    def test_same_schedule_minute_runs_once_for_each_hub_independently(self):
        fixed_now = datetime(2026, 9, 5, 9, 0, tzinfo=pinchuang.BEIJING_TZ)
        original, copied = self.hubs["pinchuang"], self.hubs["guangce"]
        for hub in (original, copied):
            hub.now = lambda: fixed_now
        with patch.object(original, "start_run") as original_run, patch.object(copied, "start_run") as copied_run:
            original._scheduler_tick()
            copied._scheduler_tick()
            original._scheduler_tick()
            copied._scheduler_tick()
        original_run.assert_called_once_with(trigger="scheduled", scheduled_time="09:00")
        copied_run.assert_called_once_with(trigger="scheduled", scheduled_time="09:00")
        self.assertIsNot(original.state["schedule_marks"], copied.state["schedule_marks"])
        self.assertIsNot(original.resume_event, copied.resume_event)
        self.assertIsNot(original.stop_event, copied.stop_event)

    def test_run_pause_resume_and_restart_keep_original_history_untouched(self):
        hub = self.hubs["guangce"]
        original_state = self.hubs["pinchuang"].state_path.read_bytes()
        with patch.object(pinchuang, "get_oss_config", return_value={"configured": True}), patch.object(pinchuang.threading.Thread, "start"):
            response = self.client.post("/api/guangce/runs")
        self.assertEqual(response.status_code, 202)
        self.assertIn("任务已创建", response.json["message"])
        self.assertTrue(hub.worker.name.startswith("guangce-run-"))
        run_id = response.json["run_id"]
        hub.worker = MagicMock()
        hub.worker.is_alive.return_value = True
        self.assertEqual(self.client.post("/api/guangce/runs/pause").json["status"], "pausing")
        self.assertEqual(self.client.post("/api/guangce/runs/resume").json["status"], "running")
        with patch.object(pinchuang, "get_oss_config", return_value={"configured": True}):
            duplicate = self.client.post("/api/guangce/runs")
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("广策中枢", duplicate.json["error"])
        self.assertEqual(self.client.get("/api/guangce/status").json["current_run"]["run_id"], run_id)
        restarted = guangce.GuangceHub(hub.config_path, hub.state_path)
        self.assertEqual(restarted.snapshot()["history"][0]["status"], "interrupted")
        self.assertEqual(json.loads(hub.state_path.read_text(encoding="utf-8"))["current_run"]["run_id"], run_id)
        self.assertEqual(self.hubs["pinchuang"].state_path.read_bytes(), original_state)

    def test_database_and_feishu_tests_use_guangce_configuration_and_name(self):
        hub = self.hubs["guangce"]
        with patch.object(pinchuang.MySQLSnapshotAdapter, "test_connection", return_value={"version": "8.0"}) as database_test:
            response = self.client.post("/api/guangce/test-database")
        self.assertEqual(response.status_code, 200)
        database_test.assert_called_once()
        self.assertEqual(hub._database_adapter().config["host"], "guangce.example.invalid")
        with patch.object(pinchuang.FeishuNotifier, "send", return_value={"sent": True}) as send:
            response = self.client.post("/api/guangce/test-feishu")
        self.assertEqual(response.status_code, 200)
        self.assertIn("模块：【广策中枢系统】", send.call_args.args[1])
        self.assertNotIn("品创", send.call_args.args[1])

    def test_invalid_config_and_idle_pause_return_errors_for_guangce_only(self):
        before = self.hubs["guangce"].config_path.read_bytes()
        self.assertEqual(self.client.post("/api/guangce/config", json={"database": {"port": 0}}).status_code, 400)
        self.assertEqual(self.hubs["guangce"].config_path.read_bytes(), before)
        for endpoint in ("pause", "resume"):
            response = self.client.post(f"/api/guangce/runs/{endpoint}")
            self.assertEqual(response.status_code, 409)
            self.assertIn("广策中枢", response.json["error"])
