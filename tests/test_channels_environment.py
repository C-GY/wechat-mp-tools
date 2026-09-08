import json
import tempfile
import unittest
import time
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from mitmproxy import http

from backend import channels, channels_refresh, mitm_proxy, pinchuang, wechat_automation


class ChannelsEnvironmentTests(unittest.TestCase):
    def setUp(self):
        mitm_proxy.reset_channels_pages()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_dir = Path(temp_dir.name) / "data" / "logs"
        path_patch = patch.object(wechat_automation, "app_dir", return_value=Path(temp_dir.name))
        path_patch.start()
        self.addCleanup(path_patch.stop)
        browser_patch = patch.object(
            wechat_automation, "find_wechat_browser_windows", return_value=[]
        )
        self.browser_windows = browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def tearDown(self):
        mitm_proxy.reset_channels_pages()

    def test_existing_native_browser_without_heartbeat_is_not_reopened(self):
        browser = {"hwnd": 6361824, "pid": 33592, "class_name": "Chrome_WidgetWin_0"}
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value=None),
            patch.object(wechat_automation, "find_wechat_browser_windows", return_value=[browser], create=True),
            patch.object(wechat_automation, "open_wechat_video_channels", return_value={}) as open_channels,
        ):
            result = wechat_automation.ensure_wechat_channels_available(
                detection_timeout=0, open_timeout=0
            )

        open_channels.assert_not_called()
        self.assertFalse(result["opened"])
        self.assertFalse(result["monitoring_active"])

    def test_minimized_browser_is_restored_without_reopening_video_channels(self):
        self.browser_windows.return_value = [{"hwnd": 42, "minimized": True}]
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value={"api_ready": True}),
            patch.object(wechat_automation, "restore_wechat_browser_window", return_value=True) as restore,
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available()
        restore.assert_called_once_with(42)
        open_channels.assert_not_called()
        self.assertTrue(result["browser_restored"])
        self.assertFalse(result["opened"])
        self.assertTrue(result["monitoring_active"])

    def test_existing_browser_without_collection_connection_stops_pipeline(self):
        self.browser_windows.return_value = [{"hwnd": 42, "minimized": False}]
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value=None),
            patch.object(wechat_automation, "restore_wechat_browser_window") as restore,
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            with self.assertRaisesRegex(RuntimeError, "未重复打开"):
                pinchuang.ensure_wechat_channels_available()
        restore.assert_not_called()
        open_channels.assert_not_called()

    def test_diagnostic_endpoint_does_not_change_proxy_or_windows(self):
        app = Flask(__name__)
        app.register_blueprint(channels.channels_bp)
        self.browser_windows.return_value = [{"hwnd": 42, "minimized": True}]
        manager = Mock(running=False)
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(wechat_automation, "restore_wechat_browser_window") as restore,
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = app.test_client().get("/api/channels/wechat/environment").get_json()
        self.assertTrue(result["browser_open"])
        self.assertFalse(result["monitoring_active"])
        manager.start.assert_not_called()
        restore.assert_not_called()
        open_channels.assert_not_called()
    def test_pipeline_waits_for_existing_page_before_trying_to_open_wechat(self):
        manager = Mock(running=True)

        def next_page_poll(since, timeout=0):
            if timeout > 0:
                return {"timestamp": since + 1, "path": "/__wx_channels_api/refresh-command"}
            return None

        with (
            patch.object(pinchuang.sys, "platform", "win32"),
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(mitm_proxy, "wait_for_channels_page", side_effect=next_page_poll),
            patch.object(
                wechat_automation, "open_wechat_video_channels", return_value={}
            ) as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available()

        self.assertTrue(result["monitoring_active"])
        open_channels.assert_not_called()

    def test_detect_button_does_not_reopen_an_available_channels_page(self):
        app = Flask(__name__)
        app.register_blueprint(channels.channels_bp)
        manager = Mock(running=True)
        activity = {"timestamp": 100.0, "path": "/__wx_channels_api/refresh-command"}

        with (
            patch.object(channels.sys, "platform", "win32"),
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value=activity),
            patch.object(
                wechat_automation, "open_wechat_video_channels", return_value={}
            ) as open_channels,
        ):
            response = app.test_client().post("/api/channels/wechat/open-channels")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["monitoring_active"])
        self.assertFalse(response.get_json()["opened"])
        open_channels.assert_not_called()
        manager.start.assert_not_called()

    def test_pipeline_reuses_a_ready_page_without_any_desktop_action(self):
        timer = threading.Timer(0.02, lambda: mitm_proxy.record_channels_page("page-1"))
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            timer.start()
            try:
                result = pinchuang.ensure_wechat_channels_available()
            finally:
                timer.join()

        self.assertFalse(result["opened"])
        self.assertTrue(result["monitoring_active"])
        open_channels.assert_not_called()

    def test_closed_page_old_online_record_does_not_skip_opening(self):
        with patch.object(mitm_proxy.time, "time", return_value=100.0):
            mitm_proxy.record_channels_page("closed-page")
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "open_wechat_video_channels", return_value={}) as open_channels,
        ):
            result = wechat_automation.ensure_wechat_channels_available(
                detection_timeout=0, open_timeout=0
            )
        self.assertTrue(result["opened"])
        self.assertFalse(result["monitoring_active"])
        open_channels.assert_called_once()

    def test_proxy_restart_reconnects_existing_page_instead_of_reopening_it(self):
        manager = Mock(running=False)

        def start_proxy():
            manager.running = True
            mitm_proxy.record_channels_page("existing-page")
            return True

        manager.start.side_effect = start_proxy
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available()

        manager.start.assert_called_once()
        open_channels.assert_not_called()
        self.assertTrue(result["proxy_started"])
        self.assertFalse(result["opened"])

    def test_unavailable_page_is_opened_once_then_readiness_is_verified(self):
        events = []

        def wait_for_page(since, timeout):
            events.append("detect")
            return {"api_ready": True} if "open" in events else None

        def open_page():
            events.append("open")
            return {"clicked": True}

        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", side_effect=wait_for_page),
            patch.object(wechat_automation, "open_wechat_video_channels", side_effect=open_page),
        ):
            result = pinchuang.ensure_wechat_channels_available()

        self.assertEqual(events.count("open"), 1)
        self.assertEqual(events[-2:], ["open", "detect"])
        self.assertTrue(result["opened"])
        self.assertTrue(result["monitoring_active"])

    def test_pipeline_does_not_continue_if_opened_page_is_not_ready(self):
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value=None),
            patch.object(wechat_automation, "open_wechat_video_channels", return_value={}) as open_channels,
        ):
            with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
                pinchuang.ensure_wechat_channels_available()

        open_channels.assert_called_once()

    def test_proxy_start_failure_never_attempts_desktop_clicks(self):
        manager = Mock(running=False)
        manager.start.return_value = False
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            with self.assertRaisesRegex(RuntimeError, "同步助手启动失败"):
                pinchuang.ensure_wechat_channels_available()
        open_channels.assert_not_called()

    def test_focus_failure_reuses_page_that_reconnected_during_open(self):
        def focus_failure():
            mitm_proxy.record_channels_page("reconnected-page")
            raise RuntimeError("微信主窗口未能获得输入焦点，请先点击微信窗口后重试")

        manager = Mock(running=True)
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(wechat_automation, "open_wechat_video_channels", side_effect=focus_failure) as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertTrue(result["monitoring_active"])
        self.assertFalse(result["opened"])
        open_channels.assert_called_once()
        manager.stop.assert_not_called()

    def test_page_reconnecting_during_window_rescan_skips_desktop_actions(self):
        def rescan():
            if self.browser_windows.call_count == 2:
                mitm_proxy.record_channels_page("reconnected-page")
            return []

        self.browser_windows.side_effect = rescan
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertTrue(result["monitoring_active"])
        open_channels.assert_not_called()

    def test_browser_appearing_during_detection_is_restored_and_rechecked(self):
        self.browser_windows.side_effect = [[], [{"hwnd": 42, "minimized": True}]]

        def restore_browser(hwnd):
            mitm_proxy.record_channels_page("restored-page")
            return True

        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "restore_wechat_browser_window", side_effect=restore_browser) as restore,
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertTrue(result["monitoring_active"])
        self.assertTrue(result["browser_restored"])
        restore.assert_called_once_with(42)
        open_channels.assert_not_called()

    def test_browser_appearing_during_reconnection_wait_is_not_opened_again(self):
        # The second wait gives the user time to open a browser. The native
        # snapshot from before that wait must not authorize another open.
        self.browser_windows.side_effect = [[], [], [{"hwnd": 42, "minimized": False}]]
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "open_wechat_video_channels", return_value={}) as open_channels,
        ):
            result = wechat_automation.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertFalse(result["monitoring_active"])
        self.assertTrue(result["browser_open"])
        self.assertFalse(result["opened"])
        open_channels.assert_not_called()

    def test_focus_failure_waits_for_a_delayed_reconnection(self):
        timer = threading.Timer(0.02, lambda: mitm_proxy.record_channels_page("delayed-page"))

        def focus_failure():
            timer.start()
            raise RuntimeError("微信主窗口未能获得输入焦点，请先点击微信窗口后重试")

        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "open_wechat_video_channels", side_effect=focus_failure) as open_channels,
        ):
            try:
                result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0.2)
            finally:
                timer.join()

        self.assertTrue(result["monitoring_active"])
        open_channels.assert_called_once()

    def test_focus_failure_with_only_stale_or_unready_pages_still_fails(self):
        with patch.object(mitm_proxy.time, "time", return_value=100.0):
            mitm_proxy.record_channels_page("closed-page")

        def focus_failure():
            mitm_proxy.record_channels_page("loading-page", api_ready=False)
            raise RuntimeError("微信主窗口未能获得输入焦点，请先点击微信窗口后重试")

        manager = Mock(running=True)
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=manager),
            patch.object(wechat_automation, "open_wechat_video_channels", side_effect=focus_failure) as open_channels,
        ):
            with self.assertRaisesRegex(RuntimeError, "采集连接尚未就绪.*输入焦点.*诊断编号") as error:
                pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        open_channels.assert_called_once()
        manager.stop.assert_not_called()
        records = [json.loads(line) for line in (self.log_dir / "channels_environment.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["event"], "unavailable")
        self.assertEqual(records[-1]["fresh_ready_page_count"], 0)
        self.assertIn(records[-1]["check_id"], str(error.exception))
        self.assertTrue(any(r["event"] == "open_failed" and "foreground_hwnd_after_open" in r for r in records))

    def test_diagnostics_omit_window_titles_and_page_identifiers(self):
        self.browser_windows.return_value = [{
            "hwnd": 42, "minimized": False, "title": "private-window-title",
            "pid": 123, "class_name": "Chrome_WidgetWin_0", "executable": "wechatappex.exe",
        }]
        mitm_proxy.record_channels_page("private-page-identifier", api_ready=False)
        with patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)):
            result = wechat_automation.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertFalse(result["monitoring_active"])
        text = (self.log_dir / "channels_environment.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private-window-title", text)
        self.assertNotIn("private-page-identifier", text)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(records[-1]["browsers"][0]["hwnd"], 42)
        self.assertIsNotNone(records[-1]["last_heartbeat_age_seconds"])

    def test_unwritable_diagnostics_do_not_break_a_ready_connection(self):
        with (
            patch.object(wechat_automation, "app_dir", side_effect=OSError("read-only installation")),
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(mitm_proxy, "wait_for_channels_page", return_value={"api_ready": True}),
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)

        self.assertTrue(result["monitoring_active"])
        open_channels.assert_not_called()

    def test_unattended_recovery_reloads_existing_browser_and_rechecks_heartbeat(self):
        self.browser_windows.return_value = [{"hwnd": 42, "minimized": False}]

        def reload_browser(window):
            mitm_proxy.record_channels_page("reloaded-page")
            return True

        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "_reload_wechat_browser_window", side_effect=reload_browser) as reload,
            patch.object(wechat_automation, "open_wechat_video_channels") as open_channels,
        ):
            result = pinchuang.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0, recover_browser=True)
        self.assertTrue(result["monitoring_active"])
        self.assertFalse(result["opened"])
        reload.assert_called_once()
        open_channels.assert_not_called()

    def test_manual_environment_check_does_not_reload_an_existing_page(self):
        self.browser_windows.return_value = [{"hwnd": 42, "minimized": False}]
        with (
            patch.object(mitm_proxy.ProxyManager, "get_instance", return_value=Mock(running=True)),
            patch.object(wechat_automation, "_reload_wechat_browser_window") as reload,
        ):
            result = wechat_automation.ensure_wechat_channels_available(detection_timeout=0, open_timeout=0)
        self.assertFalse(result["monitoring_active"])
        reload.assert_not_called()


class NativeWechatBrowserRecoveryTests(unittest.TestCase):
    def test_refresh_never_sends_keys_to_another_foreground_window(self):
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=999), keybd_event=Mock())
        with (
            patch.object(wechat_automation, "_browser_reload_attempts", {}),
            patch.object(wechat_automation.ctypes, "windll", SimpleNamespace(user32=user32)),
            patch.object(wechat_automation, "_begin_wechat_interaction", return_value={"focused": True}),
            patch.object(wechat_automation, "_end_wechat_interaction") as end,
        ):
            self.assertFalse(wechat_automation._reload_wechat_browser_window({"hwnd": 42}))
        user32.keybd_event.assert_not_called()
        end.assert_called_once()

    def test_refresh_releases_f5_and_obeys_cooldown(self):
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=42), keybd_event=Mock())
        with (
            patch.object(wechat_automation, "_browser_reload_attempts", {}),
            patch.object(wechat_automation.ctypes, "windll", SimpleNamespace(user32=user32)),
            patch.object(wechat_automation, "_begin_wechat_interaction", return_value={"focused": True}) as begin,
            patch.object(wechat_automation, "_end_wechat_interaction"),
            patch.object(wechat_automation.time, "monotonic", return_value=100),
        ):
            self.assertTrue(wechat_automation._reload_wechat_browser_window({"hwnd": 42}))
            self.assertFalse(wechat_automation._reload_wechat_browser_window({"hwnd": 42}))
        begin.assert_called_once()
        self.assertEqual([c.args for c in user32.keybd_event.call_args_list], [(0x74, 0, 0, 0), (0x74, 0, 0x0002, 0)])


class NativeWechatBrowserDetectionTests(unittest.TestCase):
    def test_detects_minimized_wechat_browser_but_not_main_window_or_chrome(self):
        windows = [
            {"hwnd": 1, "executable": "weixin.exe", "class_name": "Qt51514QWindowIcon", "title": "微信", "minimized": False},
            {"hwnd": 2, "executable": "wechatappex.exe", "class_name": "Chrome_WidgetWin_0", "title": "微信", "minimized": True},
            {"hwnd": 3, "executable": "wechatappex.exe", "class_name": "Chrome_WidgetWin_0", "title": "视频号", "minimized": False},
            {"hwnd": 4, "executable": "chrome.exe", "class_name": "Chrome_WidgetWin_0", "title": "视频号", "minimized": False},
        ]
        with patch.object(wechat_automation, "_enumerate_wechat_windows", return_value=windows):
            browsers = wechat_automation.find_wechat_browser_windows()
        self.assertEqual([window["hwnd"] for window in browsers], [3, 2])


class ChannelsPageReadinessTests(unittest.TestCase):
    def setUp(self):
        mitm_proxy.reset_channels_pages()
        channels_refresh.reset_refresh_task()

    def tearDown(self):
        mitm_proxy.reset_channels_pages()
        channels_refresh.reset_refresh_task()

    def request(self, path):
        flow = SimpleNamespace(
            request=http.Request.make("GET", "https://channels.weixin.qq.com" + path),
            response=None,
        )
        mitm_proxy.ChannelsAddon().request(flow)
        return flow

    def test_static_traffic_does_not_mean_collection_environment_is_available(self):
        self.request("/favicon.ico")
        self.assertIsNone(mitm_proxy.wait_for_channels_page(0, timeout=0))

    def test_already_open_legacy_page_can_reconnect_without_reloading(self):
        self.request("/__wx_channels_api/refresh-command")
        self.assertIsNotNone(mitm_proxy.wait_for_channels_page(0, timeout=0))

    def test_not_ready_page_does_not_claim_a_creator_refresh(self):
        channels_refresh.start_refresh_task([{"username": "author-a"}])
        self.request("/__wx_channels_api/refresh-command?page_id=a&api_ready=0&busy=0")
        self.assertIsNone(mitm_proxy.wait_for_channels_page(0, timeout=0))
        self.assertEqual(channels_refresh.get_refresh_status()["status"], "waiting")

    def test_busy_page_reports_online_without_claiming_another_task(self):
        channels_refresh.start_refresh_task([{"username": "author-a"}])
        self.request("/__wx_channels_api/refresh-command?page_id=a&api_ready=1&busy=1")
        self.assertIsNotNone(mitm_proxy.wait_for_channels_page(0, timeout=0))
        self.assertEqual(channels_refresh.get_refresh_status()["status"], "waiting")

    def test_ready_idle_page_can_claim_a_task(self):
        channels_refresh.start_refresh_task([{"username": "author-a"}])
        self.request("/__wx_channels_api/refresh-command?page_id=a&api_ready=1&busy=0")
        self.assertIsNotNone(mitm_proxy.wait_for_channels_page(0, timeout=0))
        self.assertEqual(channels_refresh.get_refresh_status()["status"], "running")

    def test_newer_not_ready_signal_invalidates_the_same_page(self):
        mitm_proxy.record_channels_page("page-a", api_ready=True)
        mitm_proxy.record_channels_page("page-a", api_ready=False)
        self.assertIsNone(mitm_proxy.wait_for_channels_page(0, timeout=0))

    def test_loading_another_page_does_not_hide_an_existing_ready_page(self):
        mitm_proxy.record_channels_page("page-a", api_ready=True)
        mitm_proxy.record_channels_page("page-b", api_ready=False)
        self.assertIsNotNone(mitm_proxy.wait_for_channels_page(0, timeout=0))

    def test_stale_page_is_not_reported_as_available(self):
        with patch.object(mitm_proxy.time, "time", return_value=100.0):
            mitm_proxy.record_channels_page("closed-page")
        self.assertIsNone(mitm_proxy.wait_for_channels_page(101.0, timeout=0))

    def test_delayed_page_poll_wakes_a_waiting_environment_check(self):
        started = time.time()
        timer = threading.Timer(0.02, lambda: mitm_proxy.record_channels_page("page-a"))
        timer.start()
        try:
            self.assertIsNotNone(mitm_proxy.wait_for_channels_page(started, timeout=1))
        finally:
            timer.join()


if __name__ == "__main__":
    unittest.main()
