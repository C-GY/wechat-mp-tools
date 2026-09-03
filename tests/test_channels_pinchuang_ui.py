import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_pinchuang.js"


class ChannelsPinchuangUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def _page(self):
        page = self.browser.new_page()
        page.set_content("<main id='app'></main>")
        page.add_script_tag(path=str(SCRIPT))
        page.evaluate(
            """() => {
                window.__saved = null;
                window.__pauseCalls = 0;
                window.__resumeCalls = 0;
                window.__runStatus = 'running';
                window.API = { pinchuang: {
                    getConfig: async () => ({
                        database: { host:'db.local', port:3306, username:'writer', database:'pinchuang_platform', has_password:true },
                        schedule: { enabled:true, times:['09:00','18:30'], creator_interval_seconds:20 },
                        feishu: { has_webhook:true, has_secret:true },
                        oss_configured:true,
                    }),
                    saveConfig: async value => { window.__saved = value; return { message:'saved' }; },
                    testDatabase: async () => ({ message:'ok', version:'8.0' }),
                    testFeishu: async () => ({ message:'sent' }),
                    startRun: async () => ({ message:'started', run_id:'run-1' }),
                    pauseRun: async () => { window.__pauseCalls++; window.__runStatus = 'paused'; return { message:'paused' }; },
                    resumeRun: async () => { window.__resumeCalls++; window.__runStatus = 'running'; return { message:'resumed' }; },
                    getStatus: async () => ({
                        running:true,
                        next_scheduled_at:'2026-09-02 18:30:00.000',
                        current_run:{
                            run_id:'run-1', status:window.__runStatus, phase:window.__runStatus === 'paused' ? 'paused' : 'uploading_oss', message:'正在同步 OSS：2/3',
                            total_creators:2, completed_creators:1, failed_creators:0,
                            current_creator_index:2, current_creator_id:'author-2', current_creator_name:'作者乙',
                            refreshed_videos:10, existing_videos:7, new_videos:3,
                            uploaded_videos:2, database_written:7, failed_items:0,
                            creators:[{ author_id:'author-1', author_name:'作者甲', status:'completed', refreshed_videos:7, existing_videos:7, new_videos:0, uploaded_videos:0, database_written:7, failed_items:0, message:'同步完成' }],
                        },
                        history:[],
                    }),
                }};
                window.Toast = { success() {}, warning() {}, error() {} };
                window.Router = { navigate() {} };
                document.getElementById('app').innerHTML = ChannelsPinchuangPage.render();
            }"""
        )
        return page

    def test_loads_persistent_config_and_progress_dashboard(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsPinchuangPage.init()")
            page.wait_for_function("document.querySelector('#pinchuang-run-message')?.textContent.includes('2/3')")
            result = page.evaluate(
                """() => ({
                    host: document.getElementById('pinchuang-db-host').value,
                    interval: document.getElementById('pinchuang-creator-interval').value,
                    times: document.getElementById('pinchuang-time-list').textContent,
                    creatorProgress: document.getElementById('pinchuang-stat-creators').textContent,
                    creatorName: document.getElementById('pinchuang-stat-creator-name').textContent,
                    currentCreator: document.getElementById('pinchuang-current-creator').textContent,
                    uploaded: document.getElementById('pinchuang-stat-uploaded').textContent,
                    row: document.getElementById('pinchuang-creator-rows').textContent,
                    oss: document.getElementById('pinchuang-oss-state').textContent,
                })"""
            )
            self.assertEqual(result["host"], "db.local")
            self.assertEqual(result["interval"], "20")
            self.assertIn("09:00", result["times"])
            self.assertEqual(result["creatorProgress"], "2/2")
            self.assertEqual(result["creatorName"], "作者乙")
            self.assertIn("正在处理第 2/2 位", result["currentCreator"])
            self.assertIn("作者乙", result["currentCreator"])
            self.assertEqual(result["uploaded"], "2")
            self.assertIn("作者甲", result["row"])
            self.assertIn("作者乙", result["row"])
            self.assertIn("第 2/2 位", result["row"])
            self.assertIn("已配置", result["oss"])
            self.assertIn("数据库处理", page.locator("#pinchuang-stat-written").locator("..").text_content())
            self.assertIn("按同步批次保留视频快照", page.locator(".page-description").text_content())
            self.assertIn("同批次重试更新原快照", page.locator(".page-description").text_content())
            self.assertIn("业务数据无变化也会生成新批次快照", page.locator("#app").text_content())
        finally:
            page.evaluate("() => ChannelsPinchuangPage.destroy()")
            page.close()

    def test_saves_multiple_daily_times_and_interval(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsPinchuangPage.init()")
            page.fill("#pinchuang-new-time", "22:15")
            page.locator("text=＋ 添加时间").click()
            page.fill("#pinchuang-creator-interval", "45")
            page.locator("#btn-pinchuang-save-schedule").click()
            page.wait_for_function("window.__saved !== null")
            saved = page.evaluate("window.__saved")
            self.assertEqual(saved["schedule"]["times"], ["09:00", "18:30", "22:15"])
            self.assertEqual(saved["schedule"]["creator_interval_seconds"], 45)
            self.assertTrue(saved["schedule"]["enabled"])
            self.assertEqual(
                page.locator("#btn-pinchuang-save-schedule").text_content(),
                "保存定时配置",
            )
        finally:
            page.evaluate("() => ChannelsPinchuangPage.destroy()")
            page.close()

    def test_running_task_can_be_paused_and_resumed(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsPinchuangPage.init()")
            pause_button = page.locator("#btn-pinchuang-pause")
            pause_button.wait_for(state="visible")
            self.assertIn("暂停", pause_button.text_content())

            pause_button.click()
            page.wait_for_function("window.__pauseCalls === 1")
            page.wait_for_function(
                "document.querySelector('#btn-pinchuang-pause')?.textContent.includes('继续')"
            )

            pause_button.click()
            page.wait_for_function("window.__resumeCalls === 1")
            page.wait_for_function(
                "document.querySelector('#btn-pinchuang-pause')?.textContent.includes('暂停')"
            )
        finally:
            page.evaluate("() => ChannelsPinchuangPage.destroy()")
            page.close()


if __name__ == "__main__":
    unittest.main()
