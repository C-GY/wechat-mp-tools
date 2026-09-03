import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "injection_scripts/src/automation.js"


class ChannelsEnvironmentHeartbeatUiTests(unittest.TestCase):
    def test_readiness_and_busy_heartbeats_continue_during_collection(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            try:
                page.set_content("<body></body>")
                page.evaluate("""() => {
                    window.__polls = [];
                    window.__nextCommand = null;
                    window.__finderCalls = 0;
                    window.WXU = {
                        API: {},
                        onInit() {},
                        request: async ({url}) => {
                            if (!url.startsWith('/__wx_channels_api/refresh-command')) return {};
                            window.__polls.push(Object.fromEntries(new URL(url, 'https://channels.weixin.qq.com').searchParams));
                            const command = window.__nextCommand;
                            window.__nextCommand = null;
                            return [null, command];
                        },
                    };
                }""")
                page.add_script_tag(path=str(SCRIPT))
                page.wait_for_function("window.__polls.length > 0")
                self.assertEqual(page.evaluate("window.__polls[0].api_ready"), "0")

                page.evaluate("""() => {
                    WXU.API.finderUserPage = () => {
                        window.__finderCalls++;
                        return new Promise(() => {});
                    };
                    window.__nextCommand = {
                        task_id: 'task-1', authors: [{username:'author-a', nickname:'A'}],
                    };
                }""")
                page.wait_for_function("window.__finderCalls === 1")
                page.wait_for_function("window.__polls.some(p => p.busy === '1')")
                busy = page.evaluate("window.__polls.find(p => p.busy === '1')")
                self.assertEqual(busy["api_ready"], "1")
                self.assertTrue(busy["page_id"])
                self.assertEqual(page.evaluate("window.__finderCalls"), 1)
            finally:
                browser.close()
