import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_oss_config.js"


class ChannelsOSSConfigUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content("<main id='app'></main>")
        self.page.add_script_tag(path=str(SCRIPT))
        self.page.evaluate("""async () => {
            window.__config = {
                access_key_id: 'saved-library', has_secret: true, configured: true,
                endpoint: 'https://oss.fandow.com',
            };
            window.__saves = [];
            window.__warnings = [];
            window.Toast = {
                success() {}, warning: message => window.__warnings.push(message),
            };
            window.API = { oss: {
                getConfig: async () => ({ ...window.__config }),
                saveConfig: async (id, secret) => {
                    window.__saves.push({ id, secret });
                    window.__config = { ...window.__config, access_key_id: id };
                    return { message: 'saved' };
                },
                clearConfig: async () => {
                    window.__config = {
                        ...window.__config, access_key_id: 'marketing-video-dashboard',
                        has_secret: false, configured: false,
                    };
                    return { message: 'cleared' };
                },
            }};
            document.getElementById('app').innerHTML = ChannelsOSSConfigPage.render();
            await ChannelsOSSConfigPage.init();
        }""")

    def tearDown(self):
        self.page.close()

    def test_preview_follows_input_but_only_save_changes_config(self):
        preview = self.page.locator("#oss-storage-base-url")
        self.assertEqual(preview.text_content(), "https://oss.fandow.com/saved-library")
        self.assertEqual(self.page.locator("#oss-access-key-secret").input_value(), "")
        self.page.fill("#oss-access-key-id", "Creator_02.v2")
        self.assertEqual(preview.text_content(), "https://oss.fandow.com/Creator_02.v2")
        self.assertEqual(self.page.evaluate("window.__saves"), [])
        self.assertEqual(self.page.evaluate("window.__config.access_key_id"), "saved-library")
        self.assertIn("ID 已变更", self.page.locator("#oss-access-key-secret").get_attribute("placeholder"))
        self.page.fill("#oss-access-key-secret", "new-test-secret")
        self.page.evaluate("() => ChannelsOSSConfigPage.save()")
        self.assertEqual(self.page.evaluate("window.__saves"), [
            {"id": "Creator_02.v2", "secret": "new-test-secret"},
        ])
        self.assertEqual(preview.text_content(), "https://oss.fandow.com/Creator_02.v2")
        self.assertEqual(self.page.locator("#oss-access-key-secret").input_value(), "")
        self.assertIn("保存后生效", self.page.locator("#app").text_content())
        self.assertIn("已有视频链接保持不变", self.page.locator("#app").text_content())

    def test_changing_id_requires_new_secret_but_unchanged_id_can_keep_it(self):
        self.page.fill("#oss-access-key-id", "next-library")
        self.page.evaluate("() => ChannelsOSSConfigPage.save()")
        self.assertEqual(self.page.evaluate("window.__saves"), [])
        self.assertIn("OSS_ACCESS_KEY_SECRET", self.page.evaluate("window.__warnings[0]"))
        self.page.fill("#oss-access-key-id", "saved-library")
        self.page.evaluate("() => ChannelsOSSConfigPage.save()")
        self.assertEqual(self.page.evaluate("window.__saves"), [{"id": "saved-library", "secret": ""}])

    def test_reload_and_clear_reset_the_preview(self):
        self.page.fill("#oss-access-key-id", "unsaved-library")
        self.page.evaluate("() => ChannelsOSSConfigPage.load()")
        self.assertEqual(self.page.locator("#oss-storage-base-url").text_content(), "https://oss.fandow.com/saved-library")
        self.page.evaluate("() => ChannelsOSSConfigPage.clear()")
        self.assertEqual(self.page.locator("#oss-storage-base-url").text_content(), "https://oss.fandow.com/marketing-video-dashboard")
        self.assertEqual(self.page.locator("#oss-access-key-id").input_value(), "marketing-video-dashboard")
        self.assertIn("请填写", self.page.locator("#oss-config-status").text_content())

    def test_invalid_paths_are_not_previewed_or_sent(self):
        for invalid in ("", "../other", "..", ".", "a/b", "a\\b", "a?b", "a#b", "a%b", "a b", '<img id="injected">'):
            with self.subTest(value=invalid):
                self.page.fill("#oss-access-key-id", invalid)
                self.assertIn("请填写有效", self.page.locator("#oss-storage-base-url").text_content())
                self.page.evaluate("() => ChannelsOSSConfigPage.save()")
                self.assertEqual(self.page.evaluate("window.__saves"), [])
                self.assertEqual(self.page.locator("#injected").count(), 0)

    def test_legacy_invalid_config_displays_error_and_allows_correction(self):
        self.page.evaluate("""async () => {
            window.__config = { ...window.__config, access_key_id: 'old/invalid',
                configured: false, configuration_error: 'OSS_ACCESS_KEY_ID 格式不合法' };
            await ChannelsOSSConfigPage.load();
        }""")
        self.assertIn("OSS_ACCESS_KEY_ID 格式不合法", self.page.locator("#oss-config-status").text_content())
        self.page.fill("#oss-access-key-id", "fixed-library")
        self.assertEqual(self.page.locator("#oss-storage-base-url").text_content(), "https://oss.fandow.com/fixed-library")


if __name__ == "__main__":
    unittest.main()
