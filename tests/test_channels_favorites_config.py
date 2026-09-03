import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from backend import channels, channels_favorites


class FavoritesConfigFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.favorites_file = self.root / "favorites.json"
        self.feeds_file = self.root / "feeds.json"
        self.authors = [
            {
                "username": "v2_author-a@finder", "nickname": "作者甲",
                "head_img_url": "", "video_url": "https://example.invalid/a",
                "added_time": 1700000000, "extra": {"tags": ["美食", "旅行"]},
            },
            {
                "username": "v2_author-b@finder", "nickname": "作者乙",
                "head_img_url": "", "video_url": "https://example.invalid/b",
                "added_time": 1700000001,
            },
        ]
        self.write_favorites(self.authors)
        self.feeds_file.write_text('{"unrelated": [{"id": "video-1"}]}', encoding="utf-8")
        self.stack.enter_context(patch.object(channels, "CHANNELS_FAVORITES_FILE", self.favorites_file))
        self.stack.enter_context(patch.object(channels, "CHANNELS_FEEDS_FILE", self.feeds_file))
        self.stack.enter_context(patch.object(channels, "get_settings", return_value={"download_dir": str(self.root)}))
        self.app = Flask(__name__)
        self.app.register_blueprint(channels.channels_bp)
        self.client = self.app.test_client()

    def write_favorites(self, data):
        self.favorites_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_favorites(self):
        return json.loads(self.favorites_file.read_text(encoding="utf-8"))


class FavoritesConfigTests(FavoritesConfigFixture):
    def test_export_and_restore_preserve_all_favorite_fields(self):
        response = self.client.post("/api/channels/favorites/export-config")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        path = Path(result["path"])
        self.assertEqual(path.parent, self.root / "channels" / "exports")
        self.assertEqual(path.suffix, ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["favorites"], self.authors)
        self.assertEqual(payload["creator_count"], 2)
        self.assertEqual(payload["format_version"], 1)
        self.assertTrue(payload["exported_at"])

        another = self.client.post("/api/channels/favorites/export-config").get_json()
        self.assertNotEqual(another["path"], result["path"])
        self.favorites_file.unlink()
        original_feeds = self.feeds_file.read_bytes()
        restored = self.client.post("/api/channels/favorites/import-config", json=payload)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["imported_count"], 2)
        self.assertEqual(self.read_favorites(), self.authors)
        self.assertEqual(self.feeds_file.read_bytes(), original_feeds)

    def test_merge_and_repeated_import_keep_local_metadata_and_distinct_ids(self):
        incoming = [
            {**self.authors[0], "nickname": "旧昵称"},
            {"username": "new-author", "nickname": "作者甲", "added_time": 123},
            {"username": "new-author", "nickname": "重复记录"},
        ]
        payload = channels_favorites.build_favorites_backup(incoming)
        response = self.client.post("/api/channels/favorites/import-config", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["imported_count"], 1)
        self.assertEqual(response.get_json()["skipped_count"], 2)
        self.assertEqual(self.read_favorites(), self.authors + [incoming[1]])
        original_bytes = self.favorites_file.read_bytes()
        with patch.object(channels, "save_favorites_atomic") as save:
            repeated = self.client.post("/api/channels/favorites/import-config", json=payload)
        self.assertEqual(repeated.get_json()["imported_count"], 0)
        self.assertEqual(repeated.get_json()["skipped_count"], 3)
        save.assert_not_called()
        self.assertEqual(self.favorites_file.read_bytes(), original_bytes)

    def test_original_favorites_file_can_be_imported(self):
        self.write_favorites([])
        response = self.client.post("/api/channels/favorites/import-config", json=self.authors)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read_favorites(), self.authors)

    def test_invalid_or_partly_invalid_backup_never_changes_favorites(self):
        valid = channels_favorites.build_favorites_backup(self.authors)
        invalid_payloads = [
            None, "wrong file", {}, [], {"favorites": self.authors},
            {**valid, "format": "other_app"},
            {**valid, "format_version": 2},
            {**valid, "format_version": True},
            {**valid, "creator_count": 99},
            {**valid, "favorites": {}},
            [{"username": "new-author"}, {"nickname": "无 ID"}],
            [{"username": "new-author"}, None],
            [{"username": "   "}], [{"username": 123}],
            [{"username": "valid", "nickname": {"invalid": True}}],
        ]
        original_bytes = self.favorites_file.read_bytes()
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/channels/favorites/import-config",
                    data=json.dumps(payload), content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(self.favorites_file.read_bytes(), original_bytes)

    def test_invalid_json_returns_a_readable_error(self):
        response = self.client.post(
            "/api/channels/favorites/import-config", data='{ "favorites":',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())
        self.assertEqual(self.read_favorites(), self.authors)

    def test_failed_replace_keeps_original_file_and_removes_temporary_file(self):
        original_bytes = self.favorites_file.read_bytes()
        with patch.object(channels_favorites.os, "replace", side_effect=PermissionError("file locked")):
            response = self.client.post(
                "/api/channels/favorites/import-config", json=[{"username": "new-author"}],
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn("原收藏未更改", response.get_json()["error"])
        self.assertEqual(self.favorites_file.read_bytes(), original_bytes)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_empty_favorites_do_not_create_an_export(self):
        self.write_favorites([])
        response = self.client.post("/api/channels/favorites/export-config")
        self.assertEqual(response.status_code, 400)
        self.assertIn("暂无已收藏", response.get_json()["error"])
        self.assertFalse((self.root / "channels").exists())

    def test_export_write_failure_returns_an_error(self):
        with patch.object(channels, "save_favorites_atomic", side_effect=OSError("disk full")):
            response = self.client.post("/api/channels/favorites/export-config")
        self.assertEqual(response.status_code, 500)
        self.assertIn("保存配置文件失败", response.get_json()["error"])
        self.assertEqual(self.read_favorites(), self.authors)

    def test_concurrent_imports_and_additions_keep_every_author(self):
        def save_author(index):
            with self.app.test_client() as client:
                author = {"username": f"concurrent-{index}"}
                if index % 2:
                    response = client.post("/api/channels/favorites", json=author)
                else:
                    response = client.post("/api/channels/favorites/import-config", json=[author])
                return response.status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(save_author, range(12)))
        self.assertEqual(statuses, [200] * 12)
        expected = {author["username"] for author in self.authors}
        expected.update(f"concurrent-{index}" for index in range(12))
        self.assertEqual({author["username"] for author in self.read_favorites()}, expected)


if __name__ == "__main__":
    unittest.main()
