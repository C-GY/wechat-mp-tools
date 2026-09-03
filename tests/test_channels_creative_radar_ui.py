import json
import mimetypes
import unittest
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_creative_radar.js"


class ChannelsCreativeRadarUiTests(unittest.TestCase):
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
                window.API = { creativeRadar: {
                    getConfig: async () => ({
                        api: { endpoint:'https://radar.example/api/external/upload', has_api_key:true,
                            timeout_seconds: window.__saved?.api?.timeout_seconds },
                        schedule: { enabled:true, times:['09:00','18:30'], creator_interval_seconds:20 },
                        feishu: { has_webhook:true, has_secret:true },
                        oss_configured:true,
                    }),
                    saveConfig: async value => { window.__saved = value; return { message:'saved' }; },
                    testApi: async () => { window.__apiCalls = (window.__apiCalls || 0) + 1; return { message:'API 地址可访问' }; },
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
                document.getElementById('app').innerHTML = ChannelsCreativeRadarPage.render();
            }"""
        )
        return page

    def test_loads_persistent_config_and_progress_dashboard(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsCreativeRadarPage.init()")
            page.wait_for_function("document.querySelector('#creative-radar-run-message')?.textContent.includes('2/3')")
            result = page.evaluate(
                """() => ({
                    host: document.getElementById('creative-radar-api-endpoint').value,
                    interval: document.getElementById('creative-radar-creator-interval').value,
                    times: document.getElementById('creative-radar-time-list').textContent,
                    creatorProgress: document.getElementById('creative-radar-stat-creators').textContent,
                    creatorName: document.getElementById('creative-radar-stat-creator-name').textContent,
                    currentCreator: document.getElementById('creative-radar-current-creator').textContent,
                    uploaded: document.getElementById('creative-radar-stat-uploaded').textContent,
                    row: document.getElementById('creative-radar-creator-rows').textContent,
                    oss: document.getElementById('creative-radar-oss-state').textContent,
                })"""
            )
            self.assertEqual(result["host"], "https://radar.example/api/external/upload")
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
            self.assertIn("API 受理", page.locator("#creative-radar-stat-written").locator("..").text_content())
            self.assertIn("分批提交全部已准备好的作品", page.locator(".page-description").text_content())
            self.assertIn("不代表逐条写入结果", page.locator("#app").text_content())
            self.assertIn("默认 600 秒（10 分钟）", page.locator("#app").text_content())
            self.assertEqual(page.locator("#creative-radar-api-timeout").input_value(), "600")
        finally:
            page.evaluate("() => ChannelsCreativeRadarPage.destroy()")
            page.close()

    def test_batch_details_show_uncertainty_and_keep_expanded_on_refresh(self):
        page = self._page()
        try:
            page.evaluate("""() => {
                window.__batchCreators = [{ author_id:'author-1', author_name:'作者甲', status:'partial', failed_items:200,
                    api_batches:[{ batch_index:1, start:1, end:200, item_count:200, status:'unconfirmed',
                        accepted_items:0, failed_items:0, unconfirmed_items:200,
                        message:'服务端超时 <img src=x onerror=alert(1)>' }],
                }];
                window.__batchRun = { total_creators:2, current_creator_index:2, current_creator_id:'author-2',
                    current_creator_name:'作者乙', status:'running', phase:'syncing_api', message:'等待 API 响应',
                    current_api_batches:[{ batch_index:1, start:1, end:3, item_count:3, status:'running' }],
                };
                ChannelsCreativeRadarPage.renderCreatorRows(window.__batchCreators, window.__batchRun, true);
            }""")
            rows = page.locator("#creative-radar-creator-rows")
            self.assertEqual(rows.locator("details").count(), 2)
            rows.locator("details summary").first.click()
            details = rows.locator("details").first
            self.assertIn("结果待确认", details.text_content())
            self.assertIn("第 1–200 条", details.text_content())
            self.assertIn("受理 0 · 明确失败 0 · 待确认 200", details.text_content())
            self.assertEqual(rows.locator("img").count(), 0)
            self.assertIn("等待响应", rows.locator("details").nth(1).text_content())
            page.evaluate("() => ChannelsCreativeRadarPage.renderCreatorRows(window.__batchCreators, window.__batchRun, true)")
            self.assertTrue(details.evaluate("element => element.open"))
        finally:
            page.close()

    def test_saves_multiple_daily_times_and_interval(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsCreativeRadarPage.init()")
            page.fill("#creative-radar-new-time", "22:15")
            page.locator("text=＋ 添加时间").click()
            page.fill("#creative-radar-creator-interval", "45")
            page.locator("#btn-creative-radar-save-schedule").click()
            page.wait_for_function("window.__saved !== null")
            saved = page.evaluate("window.__saved")
            self.assertEqual(saved["schedule"]["times"], ["09:00", "18:30", "22:15"])
            self.assertEqual(saved["schedule"]["creator_interval_seconds"], 45)
            self.assertTrue(saved["schedule"]["enabled"])
            self.assertEqual(
                page.locator("#btn-creative-radar-save-schedule").text_content(),
                "保存定时配置",
            )
        finally:
            page.evaluate("() => ChannelsCreativeRadarPage.destroy()")
            page.close()

    def test_api_config_save_and_connection_button(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsCreativeRadarPage.init()")
            page.fill("#creative-radar-api-key", "new-key")
            page.fill("#creative-radar-api-timeout", "90")
            page.locator("#btn-creative-radar-test-api").click()
            page.wait_for_function("window.__apiCalls === 1")
            saved = page.evaluate("window.__saved")
            self.assertEqual(saved["api"], {"endpoint": "https://radar.example/api/external/upload", "api_key": "new-key", "timeout_seconds": 90})
            self.assertEqual(page.locator("#creative-radar-api-timeout").input_value(), "90")
            self.assertNotIn("database", saved)
            self.assertEqual(page.locator("#creative-radar-api-key").input_value(), "")
            self.assertNotIn("MySQL", page.locator("#app").text_content())
            self.assertEqual(page.locator(".pinchuang-stats-row .stat-card").count(), 7)
        finally:
            page.evaluate("() => ChannelsCreativeRadarPage.destroy()")
            page.close()

    def test_running_task_can_be_paused_and_resumed(self):
        page = self._page()
        try:
            page.evaluate("() => ChannelsCreativeRadarPage.init()")
            pause_button = page.locator("#btn-creative-radar-pause")
            pause_button.wait_for(state="visible")
            self.assertIn("暂停", pause_button.text_content())

            pause_button.click()
            page.wait_for_function("window.__pauseCalls === 1")
            page.wait_for_function(
                "document.querySelector('#btn-creative-radar-pause')?.textContent.includes('继续')"
            )

            pause_button.click()
            page.wait_for_function("window.__resumeCalls === 1")
            page.wait_for_function(
                "document.querySelector('#btn-creative-radar-pause')?.textContent.includes('暂停')"
            )
        finally:
            page.evaluate("() => ChannelsCreativeRadarPage.destroy()")
            page.close()

    def test_sidebar_switches_between_separate_hub_pages(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            path = urlparse(route.request.url).path
            if path.startswith("/api/"):
                body = {}
                if path == "/api/creative-radar/config":
                    body = {"api": {"endpoint": "https://radar.example/api/external/upload", "has_api_key": True},
                            "schedule": {"times": ["09:00"], "creator_interval_seconds": 10}, "feishu": {}}
                elif path == "/api/pinchuang/config":
                    body = {"database": {"host": "original-db.local", "port": 3306}, "schedule": {}, "feishu": {}}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
                return
            file = ROOT / "frontend" / (path.lstrip("/") or "index.html")
            if file.is_file():
                content_type = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                route.fulfill(status=200, content_type=content_type, body=file.read_bytes())
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        try:
            page.goto("http://radar-test.local/#channels_creative_radar")
            page.locator("#creative-radar-api-endpoint").wait_for()
            self.assertEqual(page.locator("#nav-channels_creative_radar .nav-label").text_content(), "创意雷达系统")
            self.assertTrue(page.locator("#nav-channels_creative_radar").evaluate("el => el.classList.contains('active')"))
            page.locator("#nav-channels_pinchuang").click()
            page.locator("#pinchuang-db-host").wait_for()
            page.wait_for_function("document.getElementById('pinchuang-db-host').value === 'original-db.local'")
            self.assertIn("MySQL 8 配置", page.locator(".route-page:visible").text_content())
            page.locator("#nav-channels_creative_radar").click()
            page.locator("#creative-radar-api-endpoint").wait_for(state="visible")
            self.assertEqual(page.locator("#creative-radar-api-endpoint").input_value(), "https://radar.example/api/external/upload")
            self.assertNotIn("MySQL", page.locator(".route-page:visible").text_content())
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_connection_failure_shows_only_one_actionable_error(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main id='app'></main>")
            page.add_script_tag(path=str(ROOT / "frontend/js/api.js"))
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("""() => {
                window.__errors = [];
                window.Toast = {success() {}, warning() {}, error(message) { window.__errors.push(message); }};
                window.fetch = async url => new Response(JSON.stringify(
                    url.endsWith('/test-api')
                        ? {error: '无法连接 radar.example:3002：目标端口拒绝连接。请检查服务是否已启动。'}
                        : {api:{endpoint:'http://radar.example:3002/api/external/upload', has_api_key:true}, schedule:{}, feishu:{}}
                ), {status: url.endsWith('/test-api') ? 400 : 200, headers: {'Content-Type':'application/json'}});
                document.getElementById('app').innerHTML = ChannelsCreativeRadarPage.render();
            }""")
            page.evaluate("ChannelsCreativeRadarPage.testApi()")
            errors = page.evaluate("window.__errors")
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("radar.example:3002", errors[0])
            self.assertIn("服务是否已启动", errors[0])
            self.assertFalse(page.locator("#btn-creative-radar-test-api").is_disabled())
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
