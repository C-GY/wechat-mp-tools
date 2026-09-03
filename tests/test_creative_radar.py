import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from flask import Flask

from backend import creative_radar as radar, pinchuang


AUTHOR = {"username": "creator-1", "nickname": "作者甲"}
API_CONFIG = {"endpoint": "https://radar.example/api/external/upload", "api_key": "test-key"}


def video(key, **changes):
    return {"id": str(key), "description": f"作品 {key}",
            "oss_video_url": f"https://oss.fandow.com/my-bucket/{key}.mp4",
            "like_count": 7, "favorite_count": 9, "comment_count": 8,
            "share_count": 10, "duration_seconds": 125, "createtime": 1788409800,
            "cover_url": "https://example.com/cover.jpg", **changes}


def response(status=200, **body):
    result = Mock(status_code=status)
    result.json.return_value = body or {"code": 200, "data": {"inserted": [1], "updated": [], "errors": []}}
    return result


class CreativeRadarConfigTests(unittest.TestCase):
    def test_default_endpoint_uses_the_working_domain(self):
        self.assertEqual(radar.merged_config({})["api"]["endpoint"],
                         "http://pinguan-central-platform.fandow.com/api/external/upload")
        self.assertEqual(radar.merged_config({"api": API_CONFIG})["api"]["timeout_seconds"], 600)

    def test_config_secrets_persistence_and_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = pinchuang.PinchuangHub(root / "pinchuang.json", root / "pinchuang_state.json")
            original.save_config({"database": {"host": "db.local", "password": "db-secret"}})
            before = original.config_path.read_bytes()
            hub = radar.CreativeRadarHub(root / "radar.json", root / "radar_state.json")
            saved = hub.save_config({"api": {**API_CONFIG, "timeout_seconds": 90}, "schedule": {
                "enabled": True, "times": ["18:30", "09:00", "18:30"], "creator_interval_seconds": 23,
            }, "feishu": {"webhook_url": "https://example.com/bot", "secret": "bot-secret"}})
            self.assertNotIn("database", saved)
            self.assertNotIn("test-key", json.dumps(saved))
            self.assertNotIn("bot-secret", json.dumps(saved))
            self.assertTrue(saved["api"]["has_api_key"])
            self.assertEqual(saved["sync_timeout_seconds"], 90)
            self.assertEqual(saved["api"]["timeout_seconds"], 90)
            self.assertEqual(saved["schedule"]["times"], ["09:00", "18:30"])
            hub.save_config({"api": {"api_key": ""}, "feishu": {"secret": "", "webhook_url": ""}})
            reloaded = radar.CreativeRadarHub(hub.config_path, hub.state_path)
            self.assertEqual(reloaded.config["api"], {**API_CONFIG, "timeout_seconds": 90})
            self.assertEqual(reloaded.config["feishu"], {"webhook_url": "https://example.com/bot", "secret": "bot-secret"})
            self.assertEqual(reloaded.config["schedule"]["creator_interval_seconds"], 23)
            self.assertEqual(original.config_path.read_bytes(), before)
            self.assertNotEqual(hub.state_path, original.state_path)

    def test_validation_and_key_rotation(self):
        for timeout in (0, -1, 601, "abc", "", None, True, 1.5):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "同步超时"):
                radar.normalize_config({"api": {"timeout_seconds": timeout}})
        for endpoint in ("file:///tmp/upload", "http://", "https://user:pass@host/api", "http://host:bad/api", "https://ho st/api"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                radar.normalize_config({"api": {"endpoint": endpoint}})
        with self.assertRaisesRegex(ValueError, "对应的 API Key"):
            radar.normalize_config({"api": {"endpoint": "https://other.example/upload"}}, {"api": API_CONFIG})
        cleared = radar.normalize_config({"api": {"clear_api_key": True}}, {"api": API_CONFIG})
        self.assertFalse(cleared["api"]["api_key"])
        with self.assertRaisesRegex(ValueError, "API Key"):
            radar.CreativeRadarHub._validate_database_config(cleared)

    def test_routes_are_independent_and_missing_config_blocks_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = radar.CreativeRadarHub(Path(tmp) / "config.json", Path(tmp) / "state.json")
            app = Flask(__name__)
            app.register_blueprint(radar.creative_radar_bp)
            with patch.object(radar, "creative_radar_hub", hub):
                client = app.test_client()
                self.assertEqual(client.get("/api/creative-radar/config").status_code, 200)
                self.assertEqual(client.post("/api/creative-radar/runs").status_code, 400)
                saved = client.post("/api/creative-radar/config", json={"api": API_CONFIG}).get_json()
                self.assertTrue(saved["configured"])
                self.assertNotIn("api_key", saved["api"])
                self.assertEqual(client.post("/api/creative-radar/runs/pause").status_code, 409)
                self.assertEqual(client.post("/api/creative-radar/runs/resume").status_code, 409)
                self.assertFalse(client.get("/api/creative-radar/status").get_json()["running"])


class CreativeRadarAdapterTests(unittest.TestCase):
    def test_refused_connection_identifies_target_without_transport_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            refused = requests.ConnectionError(
                "HTTPConnectionPool(host='radar.example', port=3002): "
                "Max retries exceeded with url: /api/external/upload "
                "(Caused by NewConnectionError('[WinError 10061] connection refused'))"
            )
            session.options.side_effect = refused
            session.post.side_effect = refused
            adapter = radar.CreativeRadarAdapter(
                {**API_CONFIG, "endpoint": "http://radar.example:3002/api/external/upload"},
                session=session,
            )
            for operation in (adapter.test_connection, lambda: adapter.upload([])):
                with self.subTest(operation=operation), self.assertRaises(RuntimeError) as caught:
                    operation()
                message = str(caught.exception)
                self.assertIn("radar.example:3002", message)
                self.assertIn("拒绝连接", message)
                self.assertIn("服务是否已启动", message)
                self.assertNotIn("HTTPConnectionPool", message)
                self.assertNotIn("Max retries", message)

    def test_mapping_matches_reference_including_interactions_and_full_id(self):
        key = "export/" + "a" * 200
        mapped = radar.build_api_video(AUTHOR, video(key, share_url="https://channels.weixin.qq.com/example",
            oss_video_url="https://oss.fandow.com/custom-bucket/video.mp4?signature=temporary#part"))
        self.assertEqual(mapped["platform"], "wechat_channel")
        self.assertEqual(mapped["aweme_id"], key)
        self.assertEqual(mapped["video_url"], "https://oss.fandow.com/custom-bucket/video.mp4")
        self.assertEqual((mapped["like_count"], mapped["favorite_count"], mapped["share_count"], mapped["comment_count"]), (9, 7, 10, 8))
        self.assertNotIn("forward_count", mapped)
        self.assertEqual(mapped["original_url"], "https://channels.weixin.qq.com/example")
        self.assertEqual(mapped["duration"], "02:05")
        self.assertEqual(mapped["publish_time"], "2026-09-03 12:30:00")
        self.assertEqual(mapped["cover_url"], "https://example.com/cover.jpg")
        with self.assertRaisesRegex(ValueError, "OSS"):
            radar.build_api_video(AUTHOR, video("missing", oss_video_url=""))

    def test_actual_http_post_probe_checks_key_without_writing_test_video(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_OPTIONS(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                calls.append((self.path, self.headers["Content-Type"],
                              json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                payload = calls[-1][2]
                if payload["api_key"] != "test-key":
                    status, body = 403, {"code": 403, "msg": "无效的 API Key"}
                elif not payload["videos"]:
                    status, body = 400, {"code": 400, "msg": "videos 数组不能为空"}
                else:
                    status, body = 200, {"code": 200, "msg": "批次已受理"}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                adapter = radar.CreativeRadarAdapter({**API_CONFIG,
                    "endpoint": f"http://127.0.0.1:{server.server_port}/api/external/upload"})
                self.assertIn("API Key 校验成功", adapter.test_connection()["message"])
                self.assertEqual(calls, [("/api/external/upload", "application/json; charset=utf-8", {
                    "api_key": "test-key", "source": "external", "videos": [],
                })])
                payload = radar.build_api_video(AUTHOR, video("v1"))
                adapter.upload([payload])
                self.assertEqual(calls[-1], ("/api/external/upload", "application/json; charset=utf-8", {
                    "api_key": "test-key", "source": "external", "videos": [payload],
                }))
                adapter.config["api_key"] = "invalid-key"
                with self.assertRaisesRegex(RuntimeError, "无效的 API Key"):
                    adapter.test_connection()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_post_probe_rejects_unrelated_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            session.post.return_value = response(status=400, code=400, msg="source 不合法")
            adapter = radar.CreativeRadarAdapter(API_CONFIG, session=session)
            with self.assertRaisesRegex(RuntimeError, "source 不合法"):
                adapter.test_connection()
            session.options.assert_not_called()

    def test_every_run_submits_all_videos_and_continues_after_batch_failure(self):
        session = Mock()
        session.post.side_effect = [
            response(code=200, data={"errors": [{"index": 1, "reason": "暂时失败"}]}),
            response(status=503, code=503, msg="暂时不可用"),
            response(code=200, msg="批次已受理"),
        ]
        payloads = [radar.build_api_video(AUTHOR, video(i)) for i in range(401)]
        adapter = radar.CreativeRadarAdapter(API_CONFIG, session=session)
        checkpoint, progress = Mock(), Mock()
        result = adapter.sync(payloads, "batch-1", checkpoint=checkpoint, progress=progress)
        self.assertEqual(result["accepted_items"], 200)
        self.assertEqual(len(result["failures"]), 2)
        self.assertEqual(result["failures"][0]["video_id"], "1")
        unknown = result["failures"][1]
        self.assertEqual((unknown["scope"], unknown["start"], unknown["end"], unknown["item_count"]),
                         ("batch", 201, 400, 200))
        self.assertNotIn("video_id", unknown)
        self.assertEqual([batch["status"] for batch in result["batches"]], ["partial", "unconfirmed", "accepted"])
        self.assertEqual(result["batches"][1]["failed_items"], 0)
        self.assertEqual(result["batches"][1]["unconfirmed_items"], 200)
        self.assertEqual(checkpoint.call_count, 3)
        self.assertEqual([call.args[0]["status"] for call in progress.call_args_list],
                         ["running", "partial", "running", "unconfirmed", "running", "accepted"])

        def assert_full_submission():
            sent_batches = [call.kwargs["json"]["videos"] for call in session.post.call_args_list]
            self.assertEqual([len(batch) for batch in sent_batches], [200, 200, 1])
            self.assertEqual([item["aweme_id"] for batch in sent_batches for item in batch], list(map(str, range(401))))
            self.assertTrue(all(call.kwargs["timeout"] == (10, 600) for call in session.post.call_args_list))

        assert_full_submission()
        session.post.side_effect = None
        session.post.return_value = response(code=200, data={"total": 200, "inserted": 0, "updated": 200})
        # Both the same adapter and a fresh instance resend successful, unchanged
        # records as well as the earlier uncertain batch. The server owns retries.
        for adapter in (adapter, radar.CreativeRadarAdapter(API_CONFIG, session=session)):
            session.post.reset_mock()
            result = adapter.sync(payloads, "next-run", checkpoint=Mock(), progress=Mock())
            self.assertEqual((result["accepted_items"], result["failures"]), (401, []))
            assert_full_submission()

    def test_success_without_individual_results_acknowledges_the_batch(self):
        cases = [response(code=200, msg="成功"), response(code=200, data=None),
                 response(success=True), response(code=200, data={"total": 2, "inserted": 1, "updated": 1}),
                 response(code=200, data={"total": 2, "errors": []})]
        payloads = [radar.build_api_video(AUTHOR, video(i)) for i in range(2)]
        for http_response in cases:
            with self.subTest(body=http_response.json.return_value):
                session = Mock()
                session.post.return_value = http_response
                adapter = radar.CreativeRadarAdapter(API_CONFIG, session=session)
                result = adapter.sync(payloads, "batch", checkpoint=Mock(), progress=Mock())
                self.assertEqual(result["accepted_items"], 2)
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["batches"][0]["status"], "accepted")
                self.assertIn("逐条写入结果以服务端为准", result["batches"][0]["message"])

    def test_invalid_responses_and_network_failure_never_mark_success(self):
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("invalid JSON")
        cases = [invalid_json, response(code=401, msg="invalid test-key"),
                 response(code=200, data={"errors": [{"index": 5}]}),
                 response(code=200, data={"errors": [{"reason": "test-key rejected"}]}),
                 response(code=200, data={"errors": "unknown test-key failure"}),
                 response(code=200, data={"failed_count": 1}),
                 response(code=200, data={"success": False}),
                 response(code=200, data={"errors": [{"index": 0}, {"index": 0}]}),
                 requests.Timeout("network test-key timeout")]
        for index, http_result in enumerate(cases):
            with self.subTest(index=index):
                session = Mock()
                session.post.side_effect = http_result if isinstance(http_result, Exception) else None
                if not isinstance(http_result, Exception):
                    session.post.return_value = http_result
                adapter = radar.CreativeRadarAdapter(API_CONFIG, session=session)
                result = adapter.sync([radar.build_api_video(AUTHOR, video("one"))],
                    "batch", checkpoint=Mock(), progress=Mock())
                self.assertEqual(result["accepted_items"], 0)
                self.assertEqual(len(result["failures"]), 1)
                self.assertNotIn("test-key", result["failures"][0]["error"])
                self.assertNotIn("video_id", result["failures"][0])
                batch = result["batches"][0]
                self.assertEqual((batch["status"], batch["failed_items"], batch["unconfirmed_items"]), ("unconfirmed", 0, 1))

    def test_connection_probe_keeps_a_shorter_timeout(self):
        session = Mock()
        session.post.return_value = response(status=400, code=400, msg="videos 数组不能为空")
        adapter = radar.CreativeRadarAdapter({**API_CONFIG, "timeout_seconds": 90}, session=session)
        adapter.test_connection()
        self.assertEqual(session.post.call_args.kwargs["timeout"], (5, 30))
        session.post.return_value = response(code=200)
        adapter.upload([radar.build_api_video(AUTHOR, video("one"))])
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 90))


class CreativeRadarPipelineTests(unittest.TestCase):
    def test_uncertain_batch_persists_as_a_range_and_does_not_stop_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = radar.CreativeRadarHub(Path(tmp) / "config.json", Path(tmp) / "state.json")
            hub.config = radar.normalize_config({"api": API_CONFIG})
            hub.state["current_run"] = {"run_id": "run", "sync_batch_id": "batch", "status": "queued", "creators": []}
            adapter = radar.CreativeRadarAdapter(API_CONFIG, session=Mock())

            def upload(*args, **kwargs):
                payload = kwargs["json"]["videos"]
                if not payload:
                    return response(code=200)
                # The range being submitted is durable even while a long request
                # is waiting, before there is any reply from the remote service.
                current = json.loads(hub.state_path.read_text(encoding="utf-8"))["current_run"]
                batch = current["current_api_batches"][-1]
                self.assertEqual(batch["status"], "running")
                self.assertEqual(batch["item_count"], len(payload))
                if len(payload) == 200:
                    raise requests.ReadTimeout("server still processing")
                return response(code=200, msg="已受理")

            adapter.session.post.side_effect = upload
            with (patch.object(hub, "_database_adapter", return_value=adapter),
                  patch.object(pinchuang, "get_oss_config", return_value={"configured": True}),
                  patch.object(pinchuang, "ensure_wechat_channels_available", return_value={}),
                  patch.object(pinchuang, "load_json", return_value=[AUTHOR]),
                  patch.object(hub, "_refresh_author", return_value=201),
                  patch.object(hub, "_load_author_videos", return_value=[video(i) for i in range(201)]),
                  patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, [])) as oss,
                  patch.object(hub, "_notifier", return_value=Mock())):
                hub._run_pipeline("run")
            run = json.loads(hub.state_path.read_text(encoding="utf-8"))["history"][0]
            self.assertEqual((run["status"], run["database_written"], run["failed_items"]), ("partial", 1, 200))
            self.assertEqual(oss.call_args.args[2], [])
            creator = run["creators"][0]
            self.assertEqual([batch["status"] for batch in creator["api_batches"]], ["unconfirmed", "accepted"])
            self.assertEqual(len(creator["failures"]), 1)
            self.assertEqual(creator["failures"][0]["scope"], "batch")
            self.assertNotIn("video_id", creator["failures"][0])

    def test_full_pipeline_uses_api_and_continues_after_creator_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = radar.CreativeRadarHub(Path(tmp) / "config.json", Path(tmp) / "state.json")
            hub.config = radar.normalize_config({"api": API_CONFIG, "schedule": {"creator_interval_seconds": 0}})
            hub.state["current_run"] = {"run_id": "run", "sync_batch_id": "batch", "status": "queued", "creators": []}
            adapter = radar.CreativeRadarAdapter(API_CONFIG, session=Mock())
            adapter.session.post.return_value = response(code=200, msg="批次已受理")
            notifier = Mock()
            with (patch.object(hub, "_database_adapter", return_value=adapter),
                  patch.object(pinchuang, "MySQLSnapshotAdapter", side_effect=AssertionError("MySQL must not run")),
                  patch.object(pinchuang, "get_oss_config", return_value={"configured": True}),
                  patch.object(pinchuang, "ensure_wechat_channels_available", return_value={}),
                  patch.object(pinchuang, "load_json", return_value=[{"username": "fails"}, AUTHOR]),
                  patch.object(hub, "_refresh_author", side_effect=[RuntimeError("刷新失败"), 3]),
                  patch.object(hub, "_load_author_videos", side_effect=[
                      [video("old", oss_video_url="https://current/old.mp4"), video("new", oss_video_url=""), video("missing", oss_video_url="")],
                      [video("old", oss_video_url="https://current/old.mp4"), video("new"), video("missing", oss_video_url="")],
                  ]),
                  patch.object(hub, "_sync_new_videos_to_oss", return_value=(1, [{"video_id": "missing", "error": "上传失败"}])) as oss,
                  patch.object(hub, "_notifier", return_value=notifier)):
                hub._run_pipeline("run")
            run = hub.snapshot()["current_run"]
            self.assertEqual(run["status"], "partial")
            self.assertEqual(run["database_written"], 2)
            self.assertEqual(run["failed_items"], 2)
            self.assertEqual([v["id"] for v in oss.call_args.args[2]], ["new", "missing"])
            sent = adapter.session.post.call_args.kwargs["json"]["videos"]
            self.assertEqual(sent[0]["video_url"], "https://current/old.mp4")
            self.assertEqual([v["aweme_id"] for v in sent], ["old", "new"])
            self.assertTrue(all("创意雷达" in call.args[0] for call in notifier.send.call_args_list))
            self.assertEqual(hub.snapshot()["history"][0]["run_id"], "run")
            self.assertNotIn("含无变化", run["message"])
            self.assertIn("按批次受理统计", run["message"])
            persisted = json.loads(hub.state_path.read_text(encoding="utf-8"))
            batches = persisted["history"][0]["creators"][1]["api_batches"]
            self.assertEqual(batches[0]["status"], "accepted")
            self.assertEqual(batches[0]["accepted_items"], 2)
            self.assertEqual(persisted["current_run"]["current_api_batches"], batches)

    def test_schedule_and_notification_use_radar_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 9, 3, 9, 0, tzinfo=pinchuang.BEIJING_TZ)
            hub = radar.CreativeRadarHub(Path(tmp) / "config.json", Path(tmp) / "state.json", now=lambda: now)
            hub.config["schedule"] = {"enabled": True, "times": ["09:00", "18:30"], "creator_interval_seconds": 20}
            with patch.object(hub, "start_run") as start:
                hub._scheduler_tick()
                hub._scheduler_tick()
                start.assert_called_once_with(trigger="scheduled", scheduled_time="09:00")
            notifier = Mock()
            notifier.send.return_value = {"sent": True}
            with patch.object(hub, "_notifier", return_value=notifier):
                hub.test_feishu()
            self.assertIn("模块：【创意雷达系统】", notifier.send.call_args.args[1])
            self.assertIn("18:30", hub.next_scheduled_at())


if __name__ == "__main__":
    unittest.main()
