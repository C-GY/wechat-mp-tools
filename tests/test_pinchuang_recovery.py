import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from backend import pinchuang


class WechatRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.hub = pinchuang.PinchuangHub(root / "config.json", root / "state.json")
        self.hub.config = self.hub._merge_config({
            "database": {"host": "db.local", "username": "writer", "password": "test", "database": "test"},
            "schedule": {"creator_interval_seconds": 0, "enabled": True, "times": ["20:00"]},
        })
        self.hub.state["current_run"] = {
            "run_id": "run-1", "sync_batch_id": "batch-1", "status": "queued",
            "phase": "queued", "creators": [], "completed_creators": 0, "failed_creators": 0,
        }
        self.authors = [{"username": "author-a", "nickname": "A"}, {"username": "author-b", "nickname": "B"}]
        self.notifier = Mock()
        real_load = pinchuang.load_json
        for target, name, kwargs in (
            (pinchuang, "get_oss_config", {"return_value": {"configured": True}}),
            (pinchuang, "load_json", {"side_effect": lambda path, default=None: self.authors if Path(path).name == "channels_favorites.json" else real_load(path, default)}),
            (self.hub, "_database_adapter", {"return_value": Mock()}),
            (self.hub, "_notifier", {"return_value": self.notifier}),
        ):
            p = patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def creator_result(run_id, batch_id, author, adapter):
        return {"author_id": author["username"], "status": "completed", "database_written": 2, "failed_items": 0}

    def test_preflight_disconnect_retries_same_batch_without_human_action(self):
        observations = []

        def wait(run_id, seconds):
            observations.append(json.loads(self.hub.state_path.read_text(encoding="utf-8")))

        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", side_effect=[RuntimeError("focus denied"), {}, {}, {}]),
            patch.object(self.hub, "_wait_creator_interval", side_effect=wait),
            patch.object(self.hub, "_run_creator", side_effect=self.creator_result) as creator,
        ):
            self.hub._run_pipeline("run-1")

        run = self.hub.state["current_run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["sync_batch_id"], "batch-1")
        self.assertEqual(run["database_written"], 4)
        self.assertEqual(observations[0]["current_run"]["phase"], "waiting_wechat")
        self.assertTrue(observations[0]["current_run"]["wechat_recovery"]["next_retry_at"])
        self.assertEqual(observations[0]["history"], [])
        self.assertEqual(creator.call_count, 2)
        self.notifier.send.assert_not_called()

    def test_disconnect_before_second_creator_does_not_repeat_first_creator(self):
        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", side_effect=[{}, {}, RuntimeError("offline"), {}]),
            patch.object(self.hub, "_wait_creator_interval"),
            patch.object(self.hub, "_run_creator", side_effect=self.creator_result) as creator,
        ):
            self.hub._run_pipeline("run-1")
        self.assertEqual(self.hub.state["current_run"]["status"], "completed")
        self.assertEqual([c.args[2]["username"] for c in creator.call_args_list], ["author-a", "author-b"])
        self.notifier.send.assert_not_called()

    def test_shutdown_keeps_recovery_pending_instead_of_archiving_failure(self):
        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", side_effect=RuntimeError("offline")),
            patch.object(self.hub, "_wait_creator_interval", side_effect=lambda *args: self.hub.stop()),
            patch.object(self.hub, "_run_creator") as creator,
        ):
            self.hub._run_pipeline("run-1")
        saved = json.loads(self.hub.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["current_run"]["phase"], "waiting_wechat")
        self.assertEqual(saved["history"], [])
        creator.assert_not_called()
        self.notifier.send.assert_not_called()

    def test_schedule_does_not_replace_a_batch_waiting_for_recovery(self):
        self.hub.now = lambda: datetime(2026, 9, 7, 20, 0, tzinfo=pinchuang.BEIJING_TZ)
        self.hub.state["current_run"].update(status="running", phase="waiting_wechat", wechat_recovery={"attempts": 1})
        self.hub.worker = Mock()
        self.hub.worker.is_alive.return_value = True
        with patch.object(self.hub, "start_run", side_effect=RuntimeError("already running")) as start:
            self.hub._scheduler_tick()
            self.hub._scheduler_tick()
        self.assertEqual(self.hub.state["current_run"]["run_id"], "run-1")
        self.assertEqual(self.hub.state["current_run"]["merged_schedule_count"], 1)
        start.assert_not_called()
        self.notifier.send.assert_not_called()

    def test_restart_automatically_resumes_only_unfinished_creators(self):
        self.hub.state["current_run"].update(
            status="running", phase="waiting_wechat", wechat_recovery={"attempts": 4},
            creator_plan=self.authors,
            creators=[self.creator_result("run-1", "batch-1", self.authors[0], None)],
        )
        self.hub.config_path.write_text(json.dumps(self.hub.config), encoding="utf-8")
        self.hub._persist_state_locked()
        restarted = pinchuang.PinchuangHub(self.hub.config_path, self.hub.state_path)
        self.assertEqual(restarted.state["history"], [])
        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", return_value={}),
            patch.object(restarted, "_database_adapter", return_value=Mock()),
            patch.object(restarted, "_notifier", return_value=self.notifier),
            patch.object(restarted, "_scheduler_loop"),
            patch.object(restarted, "_run_creator", side_effect=self.creator_result) as creator,
        ):
            restarted.start()
            self.assertIsNotNone(restarted.worker)
            restarted.worker.join(timeout=2)
            restarted.stop()
            restarted.scheduler_thread.join(timeout=2)
        run = restarted.state["current_run"]
        self.assertEqual(run["sync_batch_id"], "batch-1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["database_written"], 4)
        self.assertEqual(run["completed_creators"], 2)
        self.assertEqual([c.args[2]["username"] for c in creator.call_args_list], ["author-b"])
        self.notifier.send.assert_not_called()

    def test_retry_interval_is_capped_and_no_writes_happen_while_offline(self):
        delays = []

        def wait(run_id, seconds):
            delays.append(seconds)
            if len(delays) == 5:
                self.hub.stop()

        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", side_effect=RuntimeError("offline")),
            patch.object(self.hub, "_wait_creator_interval", side_effect=wait),
            patch.object(self.hub, "_run_creator") as creator,
        ):
            self.hub._run_pipeline("run-1")
        self.assertEqual(delays, [15, 30, 60, 60, 60])
        creator.assert_not_called()
        self.assertEqual(self.hub.state["history"], [])

    def test_explicit_pause_stops_automatic_retries_until_resume(self):
        waiting = threading.Event()
        real_wait = self.hub._wait_creator_interval

        def pause_in_backoff(run_id, seconds):
            self.hub.pause_run()
            waiting.set()
            real_wait(run_id, 0.01)

        with (
            patch.object(pinchuang, "ensure_wechat_channels_available", side_effect=[RuntimeError("offline"), {}, {}, {}]) as ensure,
            patch.object(self.hub, "_wait_creator_interval", side_effect=pause_in_backoff),
            patch.object(self.hub, "_run_creator", side_effect=self.creator_result),
        ):
            self.hub.worker = threading.Thread(target=self.hub._run_pipeline, args=("run-1",))
            self.hub.worker.start()
            try:
                self.assertTrue(waiting.wait(1))
                deadline = time.monotonic() + 1
                while self.hub.state["current_run"]["status"] != "paused" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(self.hub.state["current_run"]["status"], "paused")
                self.assertEqual(ensure.call_count, 1)
                self.hub.resume_run()
                self.hub.worker.join(2)
                self.assertEqual(self.hub.state["current_run"]["status"], "completed")
            finally:
                self.hub.stop()
                self.hub.worker.join(2)

    def test_another_schedule_failure_cannot_overwrite_a_running_batch(self):
        self.hub.state["current_run"].update(status="running", phase="uploading_oss")
        self.hub._record_schedule_failure("20:00", "already running")
        self.assertEqual(self.hub.state["current_run"]["run_id"], "run-1")
        self.assertEqual(self.hub.state["current_run"]["phase"], "uploading_oss")
        self.assertEqual(len(self.hub.state["history"]), 1)
