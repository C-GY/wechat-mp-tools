import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
LOGIN_SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_login.js"


class VideoChannelsIconMatcherTests(unittest.TestCase):
    def test_finds_video_channels_icon_shape_in_wechat_sidebar(self):
        from backend.wechat_automation import (
            VIDEO_CHANNELS_ICON_TEMPLATE,
            find_video_channels_icon,
        )

        width, height = 72, 520
        image = [[238 for _ in range(width)] for _ in range(height)]
        left, top = 28, 298
        for y, row in enumerate(VIDEO_CHANNELS_ICON_TEMPLATE):
            for x, pixel in enumerate(row):
                if pixel == "#":
                    image[top + y][left + x] = 88

        # A solid circular distractor represents another sidebar icon.
        for y in range(100, 119):
            for x in range(28, 47):
                if (x - 37) ** 2 + (y - 109) ** 2 <= 8 ** 2:
                    image[y][x] = 88

        match = find_video_channels_icon(image)

        self.assertIsNotNone(match)
        self.assertAlmostEqual(match["x"], left + 10, delta=2)
        self.assertAlmostEqual(match["y"], top + 8, delta=2)
        self.assertGreater(match["score"], 0.8)


class WechatLauncherUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_button_opens_wechat_channels_and_reports_monitoring_ready(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(LOGIN_SCRIPT))
            page.evaluate(
                """() => {
                    window.__openWechatCalls = 0;
                    window.API = {
                        channels: {
                            openWechatChannels: async () => {
                                window.__openWechatCalls++;
                                return {
                                    monitoring_active: true,
                                    message: '微信视频号已打开，监听已就绪',
                                };
                            },
                            getProxyStatus: async () => ({
                                proxy_running: true,
                                cert_installed: true,
                            }),
                        },
                    };
                    window.Toast = { success() {}, warning() {}, error() {}, info() {} };
                    document.getElementById('app').innerHTML = ChannelsLoginPage.render();
                }"""
            )

            page.locator("#btn-open-wechat-channels").click()
            page.wait_for_function(
                "document.querySelector('#wechat-environment-status')?.textContent.includes('监听已就绪')"
            )

            result = page.evaluate(
                """() => ({
                    calls: window.__openWechatCalls,
                    disabled: document.getElementById('btn-open-wechat-channels').disabled,
                })"""
            )
            self.assertEqual(result["calls"], 1)
            self.assertFalse(result["disabled"])
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
