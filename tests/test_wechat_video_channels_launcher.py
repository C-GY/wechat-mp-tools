import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

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

    def test_finds_favorites_cube_used_as_discover_anchor(self):
        from backend.wechat_automation import (
            FAVORITES_ICON_TEMPLATE,
            find_favorites_icon,
        )

        width, height = 76, 520
        image = [[238 for _ in range(width)] for _ in range(height)]
        left, top = 27, 198
        for y, row in enumerate(FAVORITES_ICON_TEMPLATE):
            for x, pixel in enumerate(row):
                if pixel == "#":
                    image[top + y][left + x] = 88

        match = find_favorites_icon(image)

        self.assertIsNotNone(match)
        self.assertAlmostEqual(match["x"], left + 9, delta=2)
        self.assertAlmostEqual(match["y"], top + 9, delta=2)
        self.assertGreater(match["score"], 0.8)


class WechatLauncherFlowTests(unittest.TestCase):
    def test_new_layout_opens_discover_below_favorites_then_video_channels(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        discover_false_match = {"x": 36, "y": 258, "score": 0.64}
        channels = {"x": 86, "y": 152, "score": 0.88}

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(wechat_automation, "_restore_and_focus", return_value=True),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(wechat_automation, "_capture_grayscale", return_value=[[238]]),
            patch.object(
                wechat_automation,
                "find_video_channels_icon",
                side_effect=[None, discover_false_match, channels],
            ),
            patch.object(wechat_automation, "find_favorites_icon", return_value=favorites),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation.open_wechat_video_channels()

        self.assertEqual(
            click_point.call_args_list,
            [call(123, 36, 258), call(123, 86, 152)],
        )
        self.assertEqual(result["click_method"], "favorites_anchor_discover_menu")
        self.assertEqual(result["anchor_match_score"], 0.91)
        self.assertEqual(result["icon_match_score"], 0.88)

    def test_old_layout_still_clicks_direct_video_channels_entry(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        direct_channels = {"x": 36, "y": 258, "score": 0.78}

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(wechat_automation, "_restore_and_focus", return_value=True),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(wechat_automation, "_capture_grayscale", return_value=[[238]]),
            patch.object(
                wechat_automation,
                "find_video_channels_icon",
                side_effect=[direct_channels, direct_channels],
            ),
            patch.object(wechat_automation, "find_favorites_icon", return_value=favorites),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation.open_wechat_video_channels()

        click_point.assert_called_once_with(123, 36, 258)
        self.assertEqual(result["click_method"], "icon_match")


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
