import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

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
    def test_tray_hidden_main_is_restored_through_notification_icon(self):
        from backend import wechat_automation

        hidden = {"hwnd": 123, "visible": False}
        restored = {"hwnd": 123, "visible": True}
        with (
            patch.object(
                wechat_automation,
                "_invoke_wechat_notification_icon",
                return_value=True,
            ) as tray_icon,
            patch.object(wechat_automation, "_toggle_wechat_main_window_hotkey") as hotkey,
            patch.object(
                wechat_automation, "_find_wechat_window", return_value=restored
            ),
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation._restore_tray_hidden_wechat(hidden)

        tray_icon.assert_called_once_with()
        hotkey.assert_not_called()
        self.assertEqual(result, restored)

    def test_tray_restore_falls_back_to_weixin_hotkey(self):
        from backend import wechat_automation

        hidden = {"hwnd": 123, "visible": False}
        restored = {"hwnd": 123, "visible": True}
        with (
            patch.object(
                wechat_automation,
                "_invoke_wechat_notification_icon",
                return_value=False,
            ),
            patch.object(wechat_automation, "_toggle_wechat_main_window_hotkey") as hotkey,
            patch.object(
                wechat_automation, "_find_wechat_window", return_value=restored
            ),
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation._restore_tray_hidden_wechat(hidden)

        hotkey.assert_called_once_with()
        self.assertEqual(result, restored)

    def test_weixin_hotkey_always_releases_every_modifier(self):
        from backend import wechat_automation

        user32 = SimpleNamespace(keybd_event=Mock())
        with (
            patch.object(
                wechat_automation.ctypes,
                "windll",
                SimpleNamespace(user32=user32),
            ),
            patch.object(wechat_automation.time, "sleep"),
        ):
            wechat_automation._toggle_wechat_main_window_hotkey()

        self.assertEqual(
            user32.keybd_event.call_args_list,
            [
                call(0x11, 0, 0, 0),
                call(0x12, 0, 0, 0),
                call(0x57, 0, 0, 0),
                call(0x57, 0, 0x0002, 0),
                call(0x12, 0, 0x0002, 0),
                call(0x11, 0, 0x0002, 0),
            ],
        )

    def test_click_uses_real_mouse_input_instead_of_posting_qt_messages(self):
        from backend import wechat_automation

        user32 = SimpleNamespace(
            GetCursorPos=Mock(return_value=False),
            SetCursorPos=Mock(return_value=True),
            mouse_event=Mock(),
            PostMessageW=Mock(),
        )
        with (
            patch.object(
                wechat_automation.ctypes,
                "windll",
                SimpleNamespace(user32=user32),
            ),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=100, top=200),
            ),
            patch.object(wechat_automation.time, "sleep"),
        ):
            wechat_automation._click_wechat_client_point(123, 36, 258)

        user32.SetCursorPos.assert_called_once_with(136, 458)
        self.assertEqual(
            user32.mouse_event.call_args_list,
            [call(0x0002, 0, 0, 0, 0), call(0x0004, 0, 0, 0, 0)],
        )
        user32.PostMessageW.assert_not_called()

    def test_restore_does_not_attach_or_synthesize_input(self):
        from backend import wechat_automation

        user32 = SimpleNamespace(
            ShowWindowAsync=Mock(return_value=True),
            BringWindowToTop=Mock(return_value=True),
            SetForegroundWindow=Mock(return_value=True),
            IsIconic=Mock(return_value=False),
            GetForegroundWindow=Mock(return_value=123),
            AttachThreadInput=Mock(),
            keybd_event=Mock(),
            SetFocus=Mock(),
            SwitchToThisWindow=Mock(),
        )
        with patch.object(
            wechat_automation.ctypes,
            "windll",
            SimpleNamespace(user32=user32),
        ):
            result = wechat_automation._restore_and_focus(123)

        self.assertTrue(result)
        user32.ShowWindowAsync.assert_called_once_with(123, 9)
        user32.BringWindowToTop.assert_called_once_with(123)
        user32.SetForegroundWindow.assert_called_once_with(123)
        user32.AttachThreadInput.assert_not_called()
        user32.keybd_event.assert_not_called()
        user32.SetFocus.assert_not_called()
        user32.SwitchToThisWindow.assert_not_called()

    def test_main_window_lookup_includes_tray_hidden_wechat_window(self):
        from backend import wechat_automation

        hidden_main = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
            "minimized": False,
            "visible": False,
        }
        with patch.object(
            wechat_automation,
            "_enumerate_wechat_windows",
            return_value=[hidden_main],
        ) as enumerate_windows, patch.object(
            wechat_automation, "_window_normal_size", return_value=(800, 600)
        ):
            result = wechat_automation._find_wechat_window()

        enumerate_windows.assert_called_once_with(include_hidden=True)
        self.assertEqual(result["hwnd"], hidden_main["hwnd"])

    def test_main_window_lookup_ignores_small_weixin_auxiliary_window(self):
        from backend import wechat_automation

        auxiliary = {
            "hwnd": 111,
            "title": "Weixin",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
            "minimized": False,
            "visible": True,
        }
        main = {
            "hwnd": 222,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
            "minimized": True,
            "visible": True,
        }

        with patch.object(
            wechat_automation,
            "_enumerate_wechat_windows",
            return_value=[auxiliary, main],
        ), patch.object(
            wechat_automation,
            "_window_normal_size",
            side_effect=lambda hwnd: (176, 199) if hwnd == 111 else (1285, 782),
        ):
            result = wechat_automation._find_wechat_window()

        self.assertEqual(result["hwnd"], main["hwnd"])
        self.assertEqual(result["normal_size"], (1285, 782))

    def test_inactive_window_is_activated_before_navigation_clicks(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        channels = {"x": 86, "y": 152, "score": 0.88}
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=123))

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": False, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(wechat_automation, "_capture_grayscale", return_value=[[238]]),
            patch.object(wechat_automation, "find_favorites_icon", return_value=favorites),
            patch.object(
                wechat_automation,
                "find_video_channels_icon",
                return_value=channels,
            ),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
            patch.object(
                wechat_automation.ctypes,
                "windll",
                SimpleNamespace(user32=user32),
            ),
        ):
            wechat_automation.open_wechat_video_channels()

        self.assertEqual(
            click_point.call_args_list,
            [call(123, 400, 16), call(123, 36, 258), call(123, 86, 152)],
        )

    def test_discover_menu_layout_opens_discover_then_video_channels(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        channels = {"x": 86, "y": 152, "score": 0.88}

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": True, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
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
                return_value=channels,
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

    def test_classic_wechat_clicks_direct_channels_in_first_slot(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "WeChatMainWndForPC",
            "executable": "wechat.exe",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        direct_channels = {"x": 36, "y": 258, "score": 0.78}

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": True, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
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

    def test_new_weixin_qt_layout_keeps_existing_discover_menu_flow(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        # A selected Discover icon may resemble the Channels butterfly. The
        # tested new-version behavior must still click Discover first, then the
        # real Channels row in the opened menu.
        discover_false_match = {"x": 36, "y": 258, "score": 0.79}
        menu_channels = {"x": 86, "y": 152, "score": 0.88}

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": True, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(wechat_automation, "_capture_grayscale", return_value=[[238]]),
            patch.object(wechat_automation, "find_favorites_icon", return_value=favorites),
            patch.object(
                wechat_automation,
                "find_video_channels_icon",
                side_effect=[discover_false_match, menu_channels],
            ),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation.open_wechat_video_channels()

        self.assertEqual(
            click_point.call_args_list,
            [call(123, 36, 258), call(123, 86, 152)],
        )
        self.assertEqual(
            result["click_method"], "favorites_anchor_discover_menu"
        )

    def test_old_weixin_qt_layout_clicks_direct_channels_below_moments(self):
        from backend import wechat_automation

        def paint_template(image, template, left, top):
            for y, row in enumerate(template):
                for x, pixel in enumerate(row):
                    if pixel == "#":
                        image[top + y][left + x] = 88

        # Old layout reproduced from the live Weixin Qt sidebar:
        # Favorites is at y=208, Moments occupies the next slot at y=257, and
        # the direct Video Channels butterfly is one more slot down at y=305.
        nav_image = [[238 for _ in range(76)] for _ in range(560)]
        paint_template(
            nav_image,
            wechat_automation.FAVORITES_ICON_TEMPLATE,
            29,
            199,
        )
        # Non-Channels artwork in the first slot represents Moments. It must
        # not prevent the real butterfly in the second slot from being chosen.
        for y in range(249, 268):
            for x in range(29, 48):
                if (x - 38) ** 2 + (y - 258) ** 2 <= 8 ** 2:
                    nav_image[y][x] = 88
        paint_template(
            nav_image,
            wechat_automation.VIDEO_CHANNELS_ICON_TEMPLATE,
            28,
            297,
        )
        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
        }

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": True, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(
                wechat_automation,
                "_capture_grayscale",
                return_value=nav_image,
            ),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation.open_wechat_video_channels()

        click_point.assert_called_once_with(123, 38, 305)
        self.assertEqual(result["click_method"], "icon_match")

    def test_new_layout_does_not_toggle_off_an_already_selected_channels_row(self):
        from backend import wechat_automation

        window = {
            "hwnd": 123,
            "title": "微信",
            "class_name": "Qt51514QWindowIcon",
            "executable": "weixin.exe",
        }
        favorites = {"x": 36, "y": 208, "score": 0.91}
        channels = {"x": 93, "y": 152, "score": 0.83}
        nav_image = [[238 for _ in range(76)] for _ in range(560)]
        menu_image = [[238 for _ in range(320)] for _ in range(330)]
        for y in range(142, 163):
            for x in range(175, 260):
                menu_image[y][x] = 119

        with (
            patch.object(wechat_automation.sys, "platform", "win32"),
            patch.object(wechat_automation, "_find_wechat_window", return_value=window),
            patch.object(
                wechat_automation,
                "_begin_wechat_interaction",
                return_value={"focused": True, "was_topmost": False},
            ),
            patch.object(wechat_automation, "_end_wechat_interaction"),
            patch.object(
                wechat_automation,
                "_window_rect",
                return_value=SimpleNamespace(left=10, top=20, right=810, bottom=670),
            ),
            patch.object(wechat_automation, "_window_scale", return_value=1.0),
            patch.object(
                wechat_automation,
                "_capture_grayscale",
                side_effect=[nav_image, menu_image],
            ),
            patch.object(wechat_automation, "find_favorites_icon", return_value=favorites),
            patch.object(
                wechat_automation, "find_video_channels_icon", return_value=channels
            ),
            patch.object(wechat_automation, "_click_wechat_client_point") as click_point,
            patch.object(wechat_automation.time, "sleep"),
        ):
            result = wechat_automation.open_wechat_video_channels()

        click_point.assert_called_once_with(123, 36, 258)
        self.assertTrue(result["menu_already_selected"])
        self.assertEqual(
            result["click_method"],
            "favorites_anchor_discover_menu_already_selected",
        )

    def test_new_layout_matches_and_clicks_both_entries_at_multiple_window_sizes(self):
        from backend import wechat_automation

        def paint_template(image, template, left, top):
            for y, row in enumerate(template):
                for x, pixel in enumerate(row):
                    if pixel == "#":
                        image[top + y][left + x] = 88

        for width, height in ((500, 500), (700, 560), (1285, 782), (1600, 1000)):
            with self.subTest(size=(width, height)):
                captures = []

                def capture(_left, _top, capture_width, capture_height):
                    captures.append((capture_width, capture_height))
                    image = [
                        [238 for _ in range(capture_width)]
                        for _ in range(capture_height)
                    ]
                    if capture_width <= 76:
                        paint_template(
                            image,
                            wechat_automation.FAVORITES_ICON_TEMPLATE,
                            28,
                            198,
                        )
                    else:
                        paint_template(
                            image,
                            wechat_automation.VIDEO_CHANNELS_ICON_TEMPLATE,
                            84,
                            144,
                        )
                    return image

                window = {
                    "hwnd": 123,
                    "title": "微信",
                    "class_name": "Qt51514QWindowIcon",
                    "executable": "weixin.exe",
                }
                with (
                    patch.object(wechat_automation.sys, "platform", "win32"),
                    patch.object(
                        wechat_automation, "_find_wechat_window", return_value=window
                    ),
                    patch.object(
                        wechat_automation,
                        "_begin_wechat_interaction",
                        return_value={"focused": True, "was_topmost": False},
                    ),
                    patch.object(wechat_automation, "_end_wechat_interaction"),
                    patch.object(
                        wechat_automation,
                        "_window_rect",
                        return_value=SimpleNamespace(
                            left=10, top=20, right=10 + width, bottom=20 + height
                        ),
                    ),
                    patch.object(wechat_automation, "_window_scale", return_value=1.0),
                    patch.object(
                        wechat_automation, "_capture_grayscale", side_effect=capture
                    ),
                    patch.object(
                        wechat_automation, "_click_wechat_client_point"
                    ) as click_point,
                    patch.object(wechat_automation.time, "sleep"),
                ):
                    result = wechat_automation.open_wechat_video_channels()

                self.assertEqual(
                    click_point.call_args_list,
                    [call(123, 37, 257), call(123, 94, 152)],
                )
                self.assertEqual(captures[0], (76, min(height, 560)))
                self.assertEqual(captures[1], (320, min(height, 330)))
                self.assertEqual(
                    result["click_method"], "favorites_anchor_discover_menu"
                )


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
