import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from backend import oss
from backend import channels
from backend.channels_excel import write_channels_export_xlsx


class _Response:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _Session:
    def __init__(self, size):
        self.size = size
        self.calls = []

    def put(self, url, data, headers, timeout):
        uploaded = 0
        while True:
            chunk = data.read(7)
            if not chunk:
                break
            uploaded += len(chunk)
        self.calls.append(("PUT", url, headers, uploaded))
        return _Response(200)

    def head(self, url, headers, timeout):
        self.calls.append(("HEAD", url, headers, 0))
        return _Response(200, {"Content-Length": str(self.size), "ETag": '"etag-1"'})

    def get(self, url, headers, stream, timeout):
        self.calls.append(("GET", url, headers, 0))
        return _Response(206)


class OSSConfigTests(unittest.TestCase):
    def test_config_is_saved_outside_installation_and_secret_is_not_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "profile" / "oss_config.json"
            with patch.object(oss, "OSS_CONFIG_FILE", config_file):
                self.assertEqual(
                    oss.get_oss_config()["access_key_id"],
                    "marketing-video-dashboard",
                )
                saved = oss.save_oss_config("marketing-video-dashboard", "secret-value")
                self.assertTrue(saved["configured"])
                self.assertNotIn("access_key_secret", saved)
                self.assertEqual(
                    json.loads(config_file.read_text(encoding="utf-8"))["access_key_secret"],
                    "secret-value",
                )

                # An empty Secret preserves the saved one when the ID is unchanged.
                oss.save_oss_config("marketing-video-dashboard", "")
                self.assertEqual(
                    oss.get_oss_config(include_secret=True)["access_key_secret"],
                    "secret-value",
                )

    def test_config_api_never_returns_secret(self):
        app = Flask(__name__)
        app.register_blueprint(oss.oss_bp)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(oss, "OSS_CONFIG_FILE", Path(temp_dir) / "oss.json"):
                client = app.test_client()
                response = client.post(
                    "/api/oss/config",
                    json={
                        "access_key_id": "marketing-video-dashboard",
                        "access_key_secret": "top-secret",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["has_secret"])
                self.assertNotIn("top-secret", response.get_data(as_text=True))
                self.assertNotIn("top-secret", client.get("/api/oss/config").get_data(as_text=True))


class OSSUploadTests(unittest.TestCase):
    def test_upload_signs_streams_and_verifies_the_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "video.mp4"
            video.write_bytes(b"video-content")
            session = _Session(video.stat().st_size)
            progress = []
            service = oss.OSSService("access-id", "access-secret", session=session)

            result = service.upload_video(
                video,
                "video-1",
                1721174400,
                lambda uploaded, total: progress.append((uploaded, total)),
            )

            self.assertEqual(result["object_key"], "wechat_channel/2024-07-17/video-1.mp4")
            self.assertEqual(
                result["url"],
                "https://oss.fandow.com/marketing-video-dashboard/wechat_channel/2024-07-17/video-1.mp4",
            )
            self.assertEqual([call[0] for call in session.calls], ["PUT", "HEAD", "GET"])
            self.assertIn("Authorization", session.calls[0][2])
            self.assertEqual(progress[-1], (video.stat().st_size, video.stat().st_size))

    def test_queue_completion_is_persisted_back_to_the_video_feed(self):
        class _Uploader:
            def upload_video(self, file_path, material_id, scraped_at, callback):
                callback(5, 10)
                callback(10, 10)
                return {
                    "object_key": "wechat_channel/2024-07-17/video-1.mp4",
                    "url": "https://oss.example/video-1.mp4",
                    "size": 10,
                    "etag": "etag",
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tasks_file = root / "tasks.json"
            feeds_file = root / "feeds.json"
            video_file = root / "video.mp4"
            video_file.write_bytes(b"0123456789")
            feeds_file.write_text(
                json.dumps({"author-1": [{"id": "video-1", "createtime": 1721174400}]}),
                encoding="utf-8",
            )
            task = {
                "id": "task-1",
                "batch_id": "batch-1",
                "video_id": "video-1",
                "username": "author-1",
                "feed_key": "author-1",
                "author": "作者",
                "title": "作品",
                "status": "pending",
                "progress": 0,
            }
            video = {"id": "video-1", "createtime": 1721174400}
            with (
                patch.object(oss, "OSS_UPLOAD_TASKS_FILE", tasks_file),
                patch.object(channels, "CHANNELS_FEEDS_FILE", feeds_file),
                patch.object(oss.OSSService, "from_saved_config", return_value=_Uploader()),
            ):
                manager = oss.OSSUploadManager()
                manager.tasks = [task]
                with patch.object(manager, "_download_video", return_value=video_file):
                    manager._run_batch("batch-1", [(task, video)])

            self.assertEqual(manager.tasks[0]["status"], "completed")
            self.assertEqual(manager.tasks[0]["progress"], 100)
            saved_video = json.loads(feeds_file.read_text(encoding="utf-8"))["author-1"][0]
            self.assertEqual(saved_video["oss_video_url"], "https://oss.example/video-1.mp4")
            self.assertEqual(saved_video["oss_upload_status"], "completed")

    def test_single_author_sync_only_queues_that_authors_works(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            favorites_file = root / "favorites.json"
            feeds_file = root / "feeds.json"
            tasks_file = root / "tasks.json"
            favorites_file.write_text(
                json.dumps([
                    {"username": "author-a", "nickname": "作者A"},
                    {"username": "author-b", "nickname": "作者B"},
                ]),
                encoding="utf-8",
            )
            feeds_file.write_text(
                json.dumps({
                    "author-a": [
                        {"id": "video-a1", "createtime": 100},
                        {"id": "video-a2", "createtime": 300},
                    ],
                    "author-b": [{"id": "video-b1"}],
                }),
                encoding="utf-8",
            )

            with (
                patch.object(oss, "OSS_UPLOAD_TASKS_FILE", tasks_file),
                patch.object(channels, "CHANNELS_FAVORITES_FILE", favorites_file),
                patch.object(channels, "CHANNELS_FEEDS_FILE", feeds_file),
                patch.object(oss, "get_oss_config", return_value={"configured": True}),
            ):
                manager = oss.OSSUploadManager()
                with patch.object(manager, "_run_batch"):
                    result = manager.start_author_sync("author-a")
                    manager.worker.join(timeout=2)

            self.assertEqual(result["total"], 2)
            self.assertEqual({task["username"] for task in manager.tasks}, {"author-a"})
            self.assertEqual(
                [task["video_id"] for task in manager.tasks],
                ["video-a2", "video-a1"],
            )

    def test_progress_snapshot_pins_live_tasks_then_sorts_newest_first(self):
        manager = oss.OSSUploadManager()
        manager.tasks = [
            {"id": "done-new", "status": "completed", "published_at": 500},
            {"id": "pending", "status": "pending", "published_at": 200},
            {"id": "uploading", "status": "uploading", "published_at": 100},
            {"id": "done-old", "status": "completed", "published_at": 300},
            {"id": "downloading", "status": "downloading", "published_at": 150},
        ]

        snapshot = manager.snapshot()

        self.assertEqual(
            [task["id"] for task in snapshot["items"]],
            ["uploading", "downloading", "pending", "done-new", "done-old"],
        )


class OSSExcelTests(unittest.TestCase):
    def _sheet(self, configured):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook_path = Path(temp_dir) / "export.xlsx"
            write_channels_export_xlsx(
                {
                    "oss_configured": configured,
                    "creators": [{
                        "username": "author-1",
                        "nickname": "作者",
                        "videos": [{
                            "id": "video-1",
                            "video_url": "https://source.example/video.mp4",
                            "oss_video_url": "https://oss.example/video.mp4",
                        }],
                    }],
                },
                workbook_path,
            )
            with zipfile.ZipFile(workbook_path) as workbook:
                return workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")

    def test_excel_has_oss_column_and_only_fills_it_when_configured(self):
        configured = self._sheet(True)
        unconfigured = self._sheet(False)
        self.assertIn("OSS视频链接", configured)
        self.assertIn("https://oss.example/video.mp4", configured)
        self.assertIn("OSS视频链接", unconfigured)
        self.assertNotIn("https://oss.example/video.mp4", unconfigured)


if __name__ == "__main__":
    unittest.main()
