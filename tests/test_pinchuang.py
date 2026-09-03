import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend import pinchuang


class PinchuangConfigTests(unittest.TestCase):
    def test_config_is_persistent_and_secrets_are_not_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(pinchuang, "get_oss_config", return_value={"configured": True}):
                hub = pinchuang.PinchuangHub(root / "config.json", root / "state.json")
                saved = hub.save_config({
                    "database": {
                        "host": "db.example.com",
                        "port": 3306,
                        "username": "writer",
                        "password": "db-secret",
                        "database": "pinchuang_platform",
                    },
                    "schedule": {
                        "enabled": True,
                        "times": ["18:30", "09:00", "18:30"],
                        "creator_interval_seconds": 23,
                    },
                    "feishu": {
                        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret": "bot-secret",
                    },
                })

                self.assertNotIn("password", saved["database"])
                self.assertNotIn("secret", saved["feishu"])
                self.assertTrue(saved["database"]["has_password"])
                self.assertTrue(saved["feishu"]["has_secret"])
                self.assertTrue(saved["feishu"]["has_webhook"])
                self.assertNotIn("webhook_url", saved["feishu"])
                self.assertEqual(saved["schedule"]["times"], ["09:00", "18:30"])

                restarted = pinchuang.PinchuangHub(root / "config.json", root / "state2.json")
                self.assertEqual(restarted.config["database"]["password"], "db-secret")
                self.assertEqual(restarted.config["feishu"]["secret"], "bot-secret")
                self.assertEqual(restarted.config["schedule"]["creator_interval_seconds"], 23)

    def test_blank_secrets_preserve_saved_values(self):
        current = {
            "database": {"password": "db-secret"},
            "feishu": {"secret": "bot-secret"},
        }
        normalized = pinchuang.normalize_config({
            "database": {"host": "localhost", "username": "root", "password": ""},
            "feishu": {"secret": ""},
        }, current)
        self.assertEqual(normalized["database"]["password"], "db-secret")
        self.assertEqual(normalized["feishu"]["secret"], "bot-secret")


class SnapshotMappingTests(unittest.TestCase):
    def test_maps_wechat_video_to_mysql_snapshot_contract(self):
        row = pinchuang.build_snapshot_row(
            {"username": "creator-1", "nickname": "作者甲", "head_img_url": "cover"},
            {
                "id": "video-1",
                "description": "视频标题",
                "createtime": "1721188800",
                "duration_seconds": 91,
                "like_count": 12,
                "share_count": 3,
                "favorite_count": 8,
                "comment_count": 2,
                "oss_video_url": "https://oss.example/video-1.mp4",
                "cover_url": "https://cover.example/video-1.jpg",
            },
            "batch-1",
            synced_at=datetime(2026, 9, 2, 10, 30, tzinfo=pinchuang.BEIJING_TZ),
        )

        self.assertEqual(row["platform"], "wechat_channels")
        self.assertEqual(row["source_video_key"], "video-1")
        self.assertEqual(row["external_video_id"], "video-1")
        self.assertEqual(row["author_id"], "creator-1")
        self.assertEqual(row["author_name"], "作者甲")
        self.assertEqual(row["video_url"], "https://oss.example/video-1.mp4")
        self.assertEqual(row["duration_ms"], 91000)
        self.assertEqual(row["sync_batch_id"], "batch-1")
        raw = json.loads(row["raw_payload"])
        self.assertEqual(raw["video"]["cover_url"], "https://cover.example/video-1.jpg")

    def test_existing_database_url_wins_and_avoids_replacing_oss_link(self):
        row = pinchuang.build_snapshot_row(
            {"username": "creator-1", "nickname": "作者甲"},
            {"id": "video-1", "oss_video_url": "https://new.example/video.mp4"},
            "batch-2",
            existing_video_url="https://stored.example/video.mp4",
        )
        self.assertEqual(row["video_url"], "https://stored.example/video.mp4")

    def test_falls_back_to_sha256_when_platform_id_is_missing(self):
        key, external_id = pinchuang.video_source_key({
            "share_url": "https://example.com/video?a=1#fragment",
        })
        self.assertEqual(len(key), 64)
        self.assertIsNone(external_id)


class MySQLAdapterTests(unittest.TestCase):
    def test_upsert_updates_latest_batch_and_time_without_replacing_video_url(self):
        class Cursor:
            def __init__(self):
                self.sql = ""
                self.values = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def executemany(self, sql, values):
                self.sql = sql
                self.values = values

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.committed = False

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        connection = Connection()
        adapter = pinchuang.MySQLSnapshotAdapter({})
        row = {"platform": "wechat_channels", "source_video_key": "video-1"}
        with patch.object(adapter, "connect", return_value=connection):
            count = adapter.write_snapshots([row])
        update_clause = connection.cursor_instance.sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        self.assertNotIn("video_url=", update_clause)
        self.assertNotIn("created_at=", update_clause)
        self.assertNotIn("source_video_key=", update_clause)
        self.assertNotIn("platform=", update_clause)
        self.assertIn("like_count=VALUES(like_count)", update_clause)
        self.assertIn("sync_batch_id=IF(NOT (", update_clause)
        self.assertIn("synced_at=IF(NOT (", update_clause)
        self.assertIn("raw_payload=IF(NOT (", update_clause)
        self.assertIn("(like_count <=> VALUES(like_count))", update_clause)
        self.assertIn("CAST(video_title AS BINARY) <=> CAST(VALUES(video_title) AS BINARY)", update_clause)
        self.assertNotIn("video_url <=>", update_clause)
        self.assertNotIn("raw_payload <=>", update_clause)
        for field in ("sync_batch_id", "synced_at", "raw_payload"):
            self.assertLess(update_clause.index(f"{field}=IF("), update_clause.index("external_video_id=VALUES("))
        self.assertEqual(count, 1)
        self.assertTrue(connection.committed)

    def _connection_with_index(self, columns, *, non_unique=0, sub_part=None):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"version": "8.0.24"}
        cursor.fetchall.side_effect = [
            [{"column_name": name} for name in pinchuang.TARGET_COLUMNS],
            [
                {"Key_name": "video_identity", "Column_name": name,
                 "Non_unique": non_unique, "Sub_part": sub_part}
                for name in columns
            ],
        ]
        return connection

    def test_connection_accepts_full_video_identity_unique_index_in_either_order(self):
        adapter = pinchuang.MySQLSnapshotAdapter({"database": "test"})
        for columns in (("platform", "source_video_key"), ("source_video_key", "platform")):
            with self.subTest(columns=columns):
                connection = self._connection_with_index(columns)
                with patch.object(adapter, "connect", return_value=connection):
                    self.assertEqual(adapter.test_connection()["table"], pinchuang.TARGET_TABLE)
                connection.close.assert_called_once()

    def test_connection_rejects_legacy_batch_nonunique_or_prefix_index(self):
        adapter = pinchuang.MySQLSnapshotAdapter({"database": "test"})
        for columns, options in (
            (("platform", "source_video_key", "sync_batch_id"), {}),
            (("platform", "source_video_key"), {"non_unique": 1}),
            (("platform", "source_video_key"), {"sub_part": 10}),
            ((), {}),
        ):
            with self.subTest(columns=columns, options=options):
                connection = self._connection_with_index(columns, **options)
                with patch.object(adapter, "connect", return_value=connection):
                    with self.assertRaisesRegex(RuntimeError, "每个视频一行"):
                        adapter.test_connection()
                connection.close.assert_called_once()


class FeishuNotifierTests(unittest.TestCase):
    def test_payload_uses_blue_interactive_card(self):
        notifier = pinchuang.FeishuNotifier("https://example.com/hook")
        payload = notifier._payload(
            "飞书机器人发送测试消息",
            "应用：【自媒体内容采集工具】\n内容：机器人连接测试成功",
        )

        self.assertEqual(payload["msg_type"], "interactive")
        self.assertNotIn("content", payload)
        self.assertEqual(payload["card"]["header"]["template"], "blue")
        self.assertEqual(
            payload["card"]["header"]["title"]["content"],
            "【飞书机器人发送测试消息】",
        )
        self.assertEqual(
            payload["card"]["elements"][0]["text"]["content"],
            "**应用：** 【自媒体内容采集工具】\n**内容：** 机器人连接测试成功",
        )

    def test_connection_message_uses_product_module_and_second_precision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            now = datetime(2026, 9, 2, 17, 17, 2, 987000, tzinfo=pinchuang.BEIJING_TZ)
            hub = pinchuang.PinchuangHub(
                root / "config.json", root / "state.json", now=lambda: now
            )

            class Notifier:
                title = ""
                message = ""

                def configured(self):
                    return True

                def send(self, title, message):
                    self.title = title
                    self.message = message
                    return {"sent": True}

            notifier = Notifier()
            with patch.object(hub, "_notifier", return_value=notifier):
                hub.test_feishu()

            self.assertEqual(notifier.title, "飞书机器人发送测试消息")
            self.assertEqual(
                notifier.message,
                "应用：【自媒体内容采集工具】\n"
                "模块：【品创中枢系统】\n"
                "内容：机器人连接测试成功\n"
                "时间：2026-09-02 17:17:02",
            )

    def test_failed_send_retries_three_times(self):
        class Session:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                raise OSError("network down")

        session = Session()
        notifier = pinchuang.FeishuNotifier("https://example.com/hook", session=session)
        with patch.object(pinchuang.time, "sleep"):
            result = notifier.send("title", "message")
        self.assertFalse(result["sent"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(session.calls, 3)


class SchedulerTests(unittest.TestCase):
    def test_same_beijing_minute_triggers_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            now = datetime(2026, 9, 2, 9, 0, tzinfo=pinchuang.BEIJING_TZ)
            hub = pinchuang.PinchuangHub(
                root / "config.json", root / "state.json", now=lambda: now
            )
            hub.config["schedule"] = {
                "enabled": True,
                "times": ["09:00"],
                "creator_interval_seconds": 0,
            }
            calls = []
            with patch.object(hub, "start_run", side_effect=lambda **kwargs: calls.append(kwargs)):
                hub._scheduler_tick()
                hub._scheduler_tick()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["scheduled_time"], "09:00")


class PauseControlTests(unittest.TestCase):
    def test_pause_checkpoint_blocks_until_resume_and_restores_phase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hub = pinchuang.PinchuangHub(root / "config.json", root / "state.json")
            hub.state["current_run"] = {
                "run_id": "run-1",
                "status": "running",
                "phase": "checking_database",
                "message": "正在比对数据库已有作品",
            }

            class Worker:
                @staticmethod
                def is_alive():
                    return True

            hub.worker = Worker()
            paused = hub.pause_run()
            self.assertEqual(paused["status"], "pausing")
            self.assertFalse(hub.resume_event.is_set())

            checkpoint = threading.Thread(target=hub._pause_checkpoint, args=("run-1",))
            checkpoint.start()
            deadline = time.monotonic() + 2
            while (
                hub.state["current_run"]["status"] != "paused"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertEqual(hub.state["current_run"]["status"], "paused")
            self.assertTrue(checkpoint.is_alive())

            hub.resume_run()
            checkpoint.join(timeout=2)
            self.assertFalse(checkpoint.is_alive())
            self.assertEqual(hub.state["current_run"]["status"], "running")
            self.assertEqual(hub.state["current_run"]["phase"], "checking_database")
            self.assertIn("正在比对数据库已有作品", hub.state["current_run"]["message"])


class PipelineTests(unittest.TestCase):
    def _hub_with_run(self, root):
        hub = pinchuang.PinchuangHub(root / "config.json", root / "state.json")
        hub.config = pinchuang._merged_config({
            "database": {
                "host": "db.local",
                "port": 3306,
                "username": "writer",
                "password": "secret",
                "database": "pinchuang_platform",
            },
            "schedule": {"creator_interval_seconds": 0},
        })
        run = {
            "run_id": "run-1",
            "sync_batch_id": "batch-1",
            "status": "queued",
            "phase": "queued",
            "creators": [],
            "completed_creators": 0,
            "failed_creators": 0,
        }
        hub.state["current_run"] = run
        return hub

    def test_database_diff_uploads_only_new_videos_and_reuses_existing_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hub = self._hub_with_run(Path(temp_dir))
            author = {"username": "author-1", "nickname": "作者甲"}
            videos = [
                {"id": "old", "description": "old-title", "oss_video_url": "https://local/old.mp4"},
                {"id": "new", "description": "new-title", "oss_video_url": "https://oss/new.mp4"},
            ]

            class Adapter:
                written = []

                def latest_rows(self, keys):
                    self.keys = list(keys)
                    return {"old": {"video_url": "https://database/old.mp4"}}

                def write_snapshots(self, rows):
                    self.written = rows
                    return len(rows)

            adapter = Adapter()
            selected = []

            def sync_selected(run_id, selected_author, selected_videos):
                selected.extend(video["id"] for video in selected_videos)
                return 1, []

            with (
                patch.object(hub, "_refresh_author", return_value=2),
                patch.object(hub, "_load_author_videos", return_value=videos),
                patch.object(hub, "_sync_new_videos_to_oss", side_effect=sync_selected),
            ):
                result = hub._run_creator("run-1", "batch-1", author, adapter)

            self.assertEqual(selected, ["new"])
            self.assertEqual(result["existing_videos"], 1)
            self.assertEqual(result["new_videos"], 1)
            by_key = {row["source_video_key"]: row for row in adapter.written}
            self.assertEqual(by_key["old"]["video_url"], "https://database/old.mp4")
            self.assertEqual(by_key["old"]["sync_batch_id"], "batch-1")
            self.assertIsInstance(by_key["old"]["synced_at"], datetime)
            self.assertEqual(by_key["new"]["video_url"], "https://oss/new.mp4")

    def test_creator_failure_notifies_and_continues_to_next_creator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hub = self._hub_with_run(Path(temp_dir))
            authors = [
                {"username": "author-a", "nickname": "A"},
                {"username": "author-b", "nickname": "B"},
            ]
            processed = []
            notices = []

            class Adapter:
                def test_connection(self):
                    return {"version": "8.0"}

            class Notifier:
                def send(self, title, message, attempts=3):
                    notices.append((title, message))
                    return {"sent": True}

            def run_creator(run_id, batch_id, author, adapter):
                processed.append(author["username"])
                if author["username"] == "author-a":
                    raise RuntimeError("first failed")
                return {
                    "author_id": "author-b",
                    "author_name": "B",
                    "status": "completed",
                    "message": "同步完成",
                    "refreshed_videos": 1,
                    "existing_videos": 0,
                    "new_videos": 1,
                    "uploaded_videos": 1,
                    "database_written": 1,
                    "failed_items": 0,
                }

            with (
                patch.object(pinchuang, "MySQLSnapshotAdapter", return_value=Adapter()),
                patch.object(pinchuang, "get_oss_config", return_value={"configured": True}),
                patch.object(pinchuang, "ensure_wechat_channels_available", return_value={}),
                patch.object(pinchuang, "load_json", return_value=authors),
                patch.object(hub, "_notifier", return_value=Notifier()),
                patch.object(hub, "_run_creator", side_effect=run_creator),
            ):
                hub._run_pipeline("run-1")

            self.assertEqual(processed, ["author-a", "author-b"])
            self.assertEqual(hub.state["current_run"]["status"], "partial")
            self.assertEqual(hub.state["current_run"]["completed_creators"], 1)
            self.assertEqual(hub.state["current_run"]["failed_creators"], 1)
            self.assertTrue(any("创作者同步失败" in title for title, _ in notices))


if __name__ == "__main__":
    unittest.main()
