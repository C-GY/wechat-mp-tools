import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    def test_bucket_and_storage_url_are_independent_from_access_key_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(oss, "OSS_CONFIG_FILE", Path(temp_dir) / "oss.json"):
                saved = oss.save_oss_config("upload-user", "test-secret", bucket="creator-library")
                self.assertEqual(saved["access_key_id"], "upload-user")
                self.assertEqual(saved["bucket"], "creator-library")
                self.assertEqual(saved["storage_base_url"], "https://oss.fandow.com/creator-library")
                self.assertEqual(oss.get_oss_config()["bucket"], "creator-library")
                self.assertEqual(
                    oss.build_oss_public_url("wechat_channel/video-1.mp4"),
                    "https://oss.fandow.com/creator-library/wechat_channel/video-1.mp4",
                )
                self.assertNotIn("access_key_secret", saved)
                changed = oss.save_oss_config("upload-user", "", bucket="next-library")
                self.assertEqual(changed["bucket"], "next-library")
                self.assertEqual(oss.get_oss_config(include_secret=True)["access_key_secret"], "test-secret")
                with self.assertRaisesRegex(ValueError, "OSS_ACCESS_KEY_SECRET"):
                    oss.save_oss_config("another-user", "")
                # Omitted bucket on a credential-only update preserves the target.
                rotated = oss.save_oss_config("another-user", "next-secret")
                self.assertEqual(rotated["bucket"], "next-library")

    def test_old_configuration_keeps_its_original_bucket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "oss.json"
            config_file.write_text(json.dumps({
                "access_key_id": "legacy-library", "access_key_secret": "test-secret",
            }), encoding="utf-8")
            with patch.object(oss, "OSS_CONFIG_FILE", config_file):
                config = oss.get_oss_config()
                self.assertTrue(config["configured"])
                self.assertEqual(config["bucket"], "legacy-library")
                self.assertEqual(oss.OSSService.from_saved_config().bucket, "legacy-library")
                oss.save_oss_config("legacy-library", "")
                self.assertEqual(json.loads(config_file.read_text(encoding="utf-8"))["bucket"], "legacy-library")

    def test_bucket_cannot_escape_its_storage_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(oss, "OSS_CONFIG_FILE", Path(temp_dir) / "oss.json"):
                oss.save_oss_config("upload-user", "test-secret", bucket="original-library")
                for value in ("", "../other", "..", ".", "a/b", "a\\b", "a?b", "a#b", "a%b", "a b", "a\tb", "a" * 257):
                    with self.subTest(bucket=value), self.assertRaisesRegex(ValueError, "OSS_BUCKET"):
                        oss.save_oss_config("upload-user", "", bucket=value)
                    with self.subTest(service_bucket=value), self.assertRaisesRegex(ValueError, "OSS_BUCKET"):
                        oss.OSSService("upload-user", "test-secret", bucket=value)
                self.assertEqual(oss.get_oss_config()["bucket"], "original-library")

    def test_access_key_id_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(oss, "OSS_CONFIG_FILE", Path(temp_dir) / "oss.json"):
                for value in ("../other", "..", ".", "a/b", "a\\b", "a?b", "a#b", "a%b", "a b", "a\tb"):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        oss.save_oss_config(value, "test-secret")
                    with self.subTest(service_id=value), self.assertRaises(ValueError):
                        oss.OSSService(value, "test-secret")

    def test_invalid_legacy_config_can_be_read_and_corrected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "oss.json"
            config_file.write_text(json.dumps({
                "access_key_id": "old/invalid", "access_key_secret": "test-secret",
            }), encoding="utf-8")
            with patch.object(oss, "OSS_CONFIG_FILE", config_file):
                config = oss.get_oss_config()
                self.assertFalse(config["configured"])
                self.assertEqual(config["storage_base_url"], "")
                self.assertIn("OSS_ACCESS_KEY_ID", config["configuration_error"])
                self.assertNotIn("access_key_secret", config)
                with self.assertRaisesRegex(ValueError, "OSS_ACCESS_KEY_ID"):
                    oss.OSSService.from_saved_config()
                self.assertTrue(oss.save_oss_config("fixed-library", "new-secret")["configured"])

    def test_valid_bucket_names_are_not_rewritten_and_clear_restores_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(oss, "OSS_CONFIG_FILE", Path(temp_dir) / "oss.json"):
                for value in ("Library_01.v2", "library-02", "a" * 256):
                    with self.subTest(value=value):
                        config = oss.save_oss_config("upload-user", "test-secret", bucket=value)
                        self.assertEqual(config["storage_base_url"], f"https://oss.fandow.com/{value}")
                with self.assertRaises(ValueError):
                    oss.save_oss_config("a" * 257, "test-secret")
                oss.clear_oss_config()
                config = oss.get_oss_config()
                self.assertFalse(config["configured"])
                self.assertEqual(config["storage_base_url"], "https://oss.fandow.com/marketing-video-dashboard")

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
                        "bucket": "independent-library",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["has_secret"])
                self.assertEqual(response.get_json()["bucket"], "independent-library")
                self.assertEqual(client.get("/api/oss/config").get_json()["storage_base_url"],
                                 "https://oss.fandow.com/independent-library")
                self.assertNotIn("top-secret", response.get_data(as_text=True))
                self.assertNotIn("top-secret", client.get("/api/oss/config").get_data(as_text=True))
                invalid = client.post("/api/oss/config", json={
                    "access_key_id": "marketing-video-dashboard", "bucket": "../wrong",
                })
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(oss.get_oss_config()["bucket"], "independent-library")


class OSSUploadTests(unittest.TestCase):
    def test_upload_progress_is_bounded_and_completion_is_always_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "video.mp4"
            file.write_bytes(b"example video bytes")
            with patch.object(oss, "OSS_UPLOAD_TASKS_FILE", root / "tasks.json"):
                manager = oss.OSSUploadManager()
                task = {"id": "task-1", "video_id": "v1"}
                service = MagicMock()

                def upload(path, video_id, created_at, callback):
                    for value in range(101):
                        callback(value, 100)
                    return {"size": 100, "url": "https://oss.example/v1.mp4", "object_key": "v1.mp4"}

                service.upload_video.side_effect = upload
                with patch.object(oss.OSSService, "from_saved_config", return_value=service), patch.object(manager, "_download_video", return_value=file), patch.object(manager, "_save_video_result"), patch.object(manager, "_update") as update, patch.object(oss.time, "monotonic", return_value=10.0):
                    manager._run_batch("batch-1", [(task, {})])
                progress = [call.kwargs["progress"] for call in update.call_args_list if call.kwargs.get("status") == "uploading"]
                self.assertEqual(progress, [0, 100])
                self.assertEqual(update.call_args_list[-1].kwargs["status"], "completed")

    def test_remote_reuse_requires_a_public_readable_mp4_and_never_uploads(self):
        for status, size, prefix, found in ((200, 1024, b"\x00\x00\x00\x20ftypisom", True),
                                           (404, 1024, b"\x00\x00\x00\x20ftypisom", False),
                                           (200, 0, b"", False),
                                           (200, 1024, b"<html>error", False)):
            with self.subTest(status=status, size=size, prefix=prefix):
                session = MagicMock()
                head = session.head.return_value.__enter__.return_value
                head.status_code = status
                head.headers = {"Content-Length": str(size)}
                get = session.get.return_value.__enter__.return_value
                get.status_code = 206
                get.iter_content.return_value = iter([prefix])
                service = oss.OSSService("access-id", "access-secret", session=session, bucket="video-library")
                result = service.find_uploaded_video("video-1", 1721174400)
                self.assertEqual(bool(result), found)
                if found:
                    self.assertEqual(result["url"], "https://oss.fandow.com/video-library/wechat_channel/2024-07-17/video-1.mp4")
                    self.assertEqual(result["size"], size)
                session.put.assert_not_called()

    def test_upload_signs_streams_and_verifies_the_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "video.mp4"
            video.write_bytes(b"video-content")
            session = _Session(video.stat().st_size)
            progress = []
            service = oss.OSSService("access-id", "access-secret", session=session, bucket="video-library")

            result = service.upload_video(
                video,
                "video-1",
                1721174400,
                lambda uploaded, total: progress.append((uploaded, total)),
            )

            self.assertEqual(result["object_key"], "wechat_channel/2024-07-17/video-1.mp4")
            self.assertEqual(
                result["url"],
                "https://oss.fandow.com/video-library/wechat_channel/2024-07-17/video-1.mp4",
            )
            self.assertEqual(result["bucket"], "video-library")
            self.assertEqual([call[0] for call in session.calls], ["PUT", "HEAD", "GET"])
            self.assertTrue(all(call[1] == result["url"] for call in session.calls))
            self.assertIn("Authorization", session.calls[0][2])
            self.assertIn("Credential=access-id/", session.calls[0][2]["Authorization"])
            self.assertEqual(progress[-1], (video.stat().st_size, video.stat().st_size))

    def test_running_service_keeps_its_own_target_when_saved_config_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"video-content")
            with patch.object(oss, "OSS_CONFIG_FILE", root / "oss.json"):
                oss.save_oss_config("upload-user", "first-secret", bucket="first-library")
                existing_service = oss.OSSService.from_saved_config()
                existing_service.session = _Session(video.stat().st_size)
                oss.save_oss_config("upload-user", "", bucket="next-library")
                next_service = oss.OSSService.from_saved_config()
                next_service.session = _Session(video.stat().st_size)
                for service, bucket in ((existing_service, "first-library"), (next_service, "next-library")):
                    result = service.upload_video(video, "video-1")
                    self.assertEqual(result["url"], f"https://oss.fandow.com/{bucket}/wechat_channel/video-1.mp4")
                    self.assertIn("Credential=upload-user/", service.session.calls[0][2]["Authorization"])

    def test_queue_completion_is_persisted_back_to_the_video_feed(self):
        class _Uploader:
            def upload_video(self, file_path, material_id, scraped_at, callback):
                callback(5, 10)
                callback(10, 10)
                return {
                    "bucket": "creator-library",
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
            self.assertEqual(saved_video["oss_bucket"], "creator-library")

    def test_config_change_does_not_reupload_a_video_with_an_existing_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(oss, "OSS_UPLOAD_TASKS_FILE", Path(temp_dir) / "tasks.json"),
                patch.object(oss.OSSService, "from_saved_config") as service_factory,
            ):
                manager = oss.OSSUploadManager()
                old_url = "https://oss.fandow.com/old-library/wechat_channel/video-1.mp4"
                video = {"id": "video-1", "oss_video_url": old_url, "oss_upload_status": "completed"}
                candidates, batch_id = manager._build_candidates(
                    [{"username": "author-1"}], {"author-1": [video]},
                )
                manager.tasks = [task for task, _ in candidates]
                with patch.object(manager, "_download_video") as download:
                    manager._run_batch(batch_id, candidates)
                download.assert_not_called()
                service_factory.return_value.upload_video.assert_not_called()
                self.assertEqual(manager.tasks[0]["status"], "skipped")
                self.assertEqual(manager.tasks[0]["oss_url"], old_url)

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
    def _sheet(self, configured, videos=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook_path = Path(temp_dir) / "export.xlsx"
            write_channels_export_xlsx(
                {
                    "oss_configured": configured,
                    "creators": [{
                        "username": "author-1",
                        "nickname": "作者",
                        "videos": videos if videos is not None else [{
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

    def test_export_preserves_recorded_and_legacy_buckets_after_config_change(self):
        with patch.object(oss, "get_oss_config", return_value={"access_key_id": "new-library"}):
            sheet = self._sheet(True, videos=[
                {"id": "stored", "oss_video_url": "https://oss.fandow.com/original/wechat_channel/stored.mp4", "oss_bucket": "ignored-library", "oss_object_key": "wechat_channel/stored.mp4"},
                {"id": "legacy", "oss_object_key": "wechat_channel/legacy.mp4"},
                {"id": "recorded", "oss_bucket": "recorded-library", "oss_object_key": "wechat_channel/recorded.mp4"},
            ])
        self.assertIn("https://oss.fandow.com/original/wechat_channel/stored.mp4", sheet)
        self.assertIn("https://oss.fandow.com/marketing-video-dashboard/wechat_channel/legacy.mp4", sheet)
        self.assertIn("https://oss.fandow.com/recorded-library/wechat_channel/recorded.mp4", sheet)
        self.assertNotIn("https://oss.fandow.com/new-library/", sheet)


if __name__ == "__main__":
    unittest.main()
