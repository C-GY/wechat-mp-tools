import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import sync_playwright

from backend import channels


ROOT = Path(__file__).resolve().parents[1]
USER_SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_user.js"


class ChannelsAuthorExportPayloadTests(unittest.TestCase):
    def test_all_favorites_export_includes_author_metadata_and_works(self):
        payload = channels.build_authors_export_payload(
            [
                {"username": "author-a", "nickname": "A", "added_time": 1},
                {"username": "author-b", "nickname": "B", "added_time": 2},
            ],
            {
                "author-a": [{"id": "video-1"}, {"id": "video-2"}],
                # Legacy feed files can be keyed by nickname.
                "B": [{"id": "video-3"}],
            },
        )

        self.assertEqual(payload["scope"], "all_favorites")
        self.assertEqual(payload["creator_count"], 2)
        self.assertEqual(payload["video_count"], 3)
        self.assertEqual(payload["creators"][0]["added_time"], 1)
        self.assertEqual(payload["creators"][1]["videos"][0]["id"], "video-3")

    def test_single_author_export_only_contains_requested_author(self):
        payload = channels.build_authors_export_payload(
            [
                {"username": "author-a", "nickname": "A"},
                {"username": "author-b", "nickname": "B"},
            ],
            {
                "author-a": [{"id": "video-1"}],
                "author-b": [{"id": "video-2"}],
            },
            username="author-b",
        )

        self.assertEqual(payload["scope"], "single_author")
        self.assertEqual(payload["creator_count"], 1)
        self.assertEqual(payload["creators"][0]["username"], "author-b")
        self.assertEqual(payload["creators"][0]["videos"][0]["id"], "video-2")

    def test_export_endpoint_writes_excel_to_channels_exports_directory(self):
        app = Flask(__name__)
        app.register_blueprint(channels.channels_bp)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            favorites_file = root / "favorites.json"
            feeds_file = root / "feeds.json"
            favorites_file.write_text(
                json.dumps([{
                    "username": "author-a",
                    "nickname": "作者A",
                    "head_img_url": "https://example.invalid/avatar.jpg",
                    "raw_creator_extra": "not-for-creator-export",
                }]),
                encoding="utf-8",
            )
            feeds_file.write_text(
                json.dumps({"author-a": [{
                    "id": "video-1",
                    "description": "示例作品",
                    "video_url": "https://example.invalid/video.mp4",
                    "video_url_h264": "https://example.invalid/video-h264.mp4",
                    "video_url_h265": "https://example.invalid/video-h265.mp4",
                    "decode_key": "secret-key",
                    "raw_extra": "not-for-export",
                }]}),
                encoding="utf-8",
            )

            with (
                patch.object(channels, "CHANNELS_FAVORITES_FILE", favorites_file),
                patch.object(channels, "CHANNELS_FEEDS_FILE", feeds_file),
                patch.object(channels, "get_settings", return_value={"download_dir": str(root)}),
            ):
                response = app.test_client().post(
                    "/api/channels/export-authors",
                    json={"username": "author-a"},
                )

            self.assertEqual(response.status_code, 200)
            result = response.get_json()
            export_path = Path(result["path"])
            self.assertTrue(export_path.is_file())
            self.assertEqual(export_path.suffix, ".xlsx")
            self.assertEqual(result["format"], "xlsx")
            self.assertEqual(export_path.parent, root / "channels" / "exports")
            with zipfile.ZipFile(export_path) as workbook:
                self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
                self.assertIn("xl/worksheets/sheet2.xml", workbook.namelist())
                self.assertIn("xl/worksheets/_rels/sheet1.xml.rels", workbook.namelist())
                self.assertIn("xl/worksheets/_rels/sheet2.xml.rels", workbook.namelist())
                creators_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
                videos_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("作者A", creators_xml)
                self.assertNotIn("其他创作者数据", creators_xml)
                self.assertNotIn("not-for-creator-export", creators_xml)
                self.assertIn("video-1", videos_xml)
                self.assertIn("示例作品", videos_xml)
                self.assertNotIn("H264 视频链接", videos_xml)
                self.assertNotIn("H265 视频链接", videos_xml)
                self.assertNotIn("解密密钥", videos_xml)
                self.assertNotIn("其他作品数据", videos_xml)
                self.assertNotIn("secret-key", videos_xml)
                self.assertNotIn("not-for-export", videos_xml)


class ChannelsAuthorExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_all_and_current_author_export_buttons_use_the_expected_scope(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(USER_SCRIPT))
            page.evaluate(
                """() => {
                    window.__exportCalls = [];
                    window.__openedPaths = [];
                    window.__messages = [];
                    window.API = {
                        channels: {
                            exportAuthors: async (username) => {
                                window.__exportCalls.push(username);
                                return {
                                    path: `C:/exports/${username || 'all'}.xlsx`,
                                    creator_count: username ? 1 : 2,
                                    video_count: username ? 3 : 5,
                                };
                            },
                            openParent: async (path) => {
                                window.__openedPaths.push(path);
                            },
                        },
                    };
                    window.Toast = {
                        success(message) { window.__messages.push(message); },
                        warning(message) { window.__messages.push(message); },
                        error(message) { window.__messages.push(message); },
                    };
                    window.Router = { navigate() {} };
                    document.getElementById('app').innerHTML = ChannelsUserPage.render();
                    document.getElementById('channels-user-selector-section').style.display = 'block';
                    document.getElementById('channels-user-profile-section').style.display = 'block';
                    ChannelsUserPage.username = 'author-a';
                }"""
            )

            page.locator("#btn-export-all-authors").click()
            page.wait_for_function("window.__exportCalls.length === 1")
            page.locator("#btn-export-current-author").click()
            page.wait_for_function("window.__exportCalls.length === 2")

            result = page.evaluate(
                """() => ({
                    exportCalls: window.__exportCalls,
                    openedPaths: window.__openedPaths,
                    messages: window.__messages,
                    allButtonText: document.getElementById('btn-export-all-authors').textContent.trim(),
                    currentButtonText: document.getElementById('btn-export-current-author').textContent.trim(),
                })"""
            )
            self.assertEqual(result["exportCalls"], ["", "author-a"])
            self.assertEqual(
                result["openedPaths"],
                ["C:/exports/all.xlsx", "C:/exports/author-a.xlsx"],
            )
            self.assertIn("导出全部创作者 Excel", result["allButtonText"])
            self.assertIn("导出当前创作者 Excel", result["currentButtonText"])
            self.assertEqual(len(result["messages"]), 2)
        finally:
            page.close()

    def test_current_author_oss_sync_refreshes_and_shows_open_link(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(USER_SCRIPT))
            page.evaluate(
                """() => {
                    window.__syncCalls = [];
                    window.__messages = [];
                    window.API = {
                        oss: {
                            syncAuthor: async (username) => {
                                window.__syncCalls.push(username);
                                return { batch_id: 'batch-author-a', total: 1 };
                            },
                            getUploads: async () => ({
                                running: false,
                                items: [{
                                    batch_id: 'batch-author-a',
                                    status: 'completed',
                                }],
                            }),
                        },
                        channels: {
                            getAuthorVideos: async () => [{
                                id: 'video-a1',
                                description: '已上传作品',
                                oss_video_url: 'https://oss.example/video-a1.mp4',
                            }],
                        },
                    };
                    window.Toast = {
                        success(message) { window.__messages.push(message); },
                        warning(message) { window.__messages.push(message); },
                        error(message) { window.__messages.push(message); },
                    };
                    window.Router = { navigate() {} };
                    document.getElementById('app').innerHTML = ChannelsUserPage.render();
                    document.getElementById('channels-user-profile-section').style.display = 'block';
                    ChannelsUserPage.username = 'author-a';
                    ChannelsUserPage.history = [];
                    ChannelsUserPage.videos = [{ id: 'video-a1', description: '待上传作品' }];
                    ChannelsUserPage.renderVideos();
                }"""
            )

            page.locator("#btn-sync-current-author-oss").click()
            page.wait_for_selector("a[title='打开 OSS 视频']", timeout=3000)

            result = page.evaluate(
                """() => ({
                    syncCalls: window.__syncCalls,
                    href: document.querySelector("a[title='打开 OSS 视频']").href,
                    buttonText: document.getElementById('btn-sync-current-author-oss').textContent.trim(),
                    buttonDisabled: document.getElementById('btn-sync-current-author-oss').disabled,
                    messages: window.__messages,
                })"""
            )
            self.assertEqual(result["syncCalls"], ["author-a"])
            self.assertEqual(result["href"], "https://oss.example/video-a1.mp4")
            self.assertIn("同步当前作者 OSS", result["buttonText"])
            self.assertFalse(result["buttonDisabled"])
            self.assertTrue(any("同步完成" in message for message in result["messages"]))
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
