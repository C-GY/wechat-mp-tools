import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_accounts.js"


class ChannelsAccountsRefreshUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_one_click_refresh_tracks_until_completion(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(ACCOUNTS_SCRIPT))
            page.evaluate(
                """() => {
                    window.__callOrder = [];
                    window.__openWechatCalls = 0;
                    window.__refreshStartCalls = 0;
                    window.API = {
                        channels: {
                            openWechatChannels: async () => {
                                window.__openWechatCalls++;
                                window.__callOrder.push('open');
                                return {
                                    monitoring_active: true,
                                    message: '微信视频号已打开，监听已就绪',
                                };
                            },
                            startFavoritesRefresh: async () => {
                                window.__refreshStartCalls++;
                                window.__callOrder.push('refresh');
                                return {
                                    task_id: 'task-1', status: 'waiting', total_authors: 2,
                                    completed_authors: 0, failed_authors: 0, total_videos: 0,
                                    message: '等待微信视频号页面响应', created: true,
                                };
                            },
                            getFavoritesRefreshStatus: async () => ({
                                task_id: 'task-1', status: 'completed', total_authors: 2,
                                completed_authors: 2, failed_authors: 0, total_videos: 36,
                                message: '刷新完成：成功 2 个作者',
                            }),
                            getFavorites: async () => [],
                        },
                    };
                    window.Toast = { success() {}, warning() {}, error() {} };
                    document.getElementById('app').innerHTML = ChannelsAccountsPage.render();
                    ChannelsAccountsPage.favorites = [
                        { username: 'author-a', nickname: 'A' },
                        { username: 'author-b', nickname: 'B' },
                    ];
                }"""
            )

            page.locator("#btn-refresh-all-favorites").click()
            page.wait_for_function(
                "document.querySelector('#favorites-refresh-status-text')?.textContent.includes('成功 2 个作者')"
            )

            result = page.evaluate(
                """() => ({
                    openCalls: window.__openWechatCalls,
                    startCalls: window.__refreshStartCalls,
                    callOrder: window.__callOrder,
                    statusVisible: document.getElementById('favorites-refresh-status').style.display !== 'none',
                    buttonDisabled: document.getElementById('btn-refresh-all-favorites').disabled,
                })"""
            )
            self.assertEqual(result["openCalls"], 1)
            self.assertEqual(result["startCalls"], 1)
            self.assertEqual(result["callOrder"], ["open", "refresh"])
            self.assertTrue(result["statusVisible"])
            self.assertFalse(result["buttonDisabled"])
        finally:
            page.close()

    def test_one_click_refresh_reports_open_failure_without_starting_task(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(ACCOUNTS_SCRIPT))
            page.evaluate(
                """() => {
                    window.__refreshStartCalls = 0;
                    window.__errors = [];
                    window.API = {
                        channels: {
                            openWechatChannels: async () => {
                                throw new Error('未检测到正在运行的微信客户端');
                            },
                            startFavoritesRefresh: async () => {
                                window.__refreshStartCalls++;
                            },
                        },
                    };
                    window.Toast = {
                        success() {}, warning() {},
                        error(message) { window.__errors.push(message); },
                    };
                    document.getElementById('app').innerHTML = ChannelsAccountsPage.render();
                    ChannelsAccountsPage.favorites = [
                        { username: 'author-a', nickname: 'A' },
                    ];
                }"""
            )

            page.locator("#btn-refresh-all-favorites").click()
            page.wait_for_function(
                "document.querySelector('#favorites-refresh-status-text')?.textContent.includes('未检测到正在运行的微信客户端')"
            )

            result = page.evaluate(
                """() => ({
                    startCalls: window.__refreshStartCalls,
                    errors: window.__errors,
                    buttonDisabled: document.getElementById('btn-refresh-all-favorites').disabled,
                    state: document.getElementById('favorites-refresh-status').dataset.state,
                })"""
            )
            self.assertEqual(result["startCalls"], 0)
            self.assertIn("未检测到正在运行的微信客户端", result["errors"][-1])
            self.assertFalse(result["buttonDisabled"])
            self.assertEqual(result["state"], "failed")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
