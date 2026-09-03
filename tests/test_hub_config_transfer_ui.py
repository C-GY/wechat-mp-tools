import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from test_hub_config_transfer import HubConfigFixture


ROOT = Path(__file__).resolve().parents[1]
PAGES = {"pinchuang": "ChannelsPinchuangPage", "creative-radar": "ChannelsCreativeRadarPage"}


class HubConfigTransferUiTests(HubConfigFixture):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page_for(self, key):
        page = self.browser.new_page(viewport={"width": 1280, "height": 960})
        self.addCleanup(page.close)

        def handle(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/":
                route.fulfill(content_type="text/html", body="""
                    <main id="app" style="padding:24px;"></main>
                    <div class="modal-overlay" id="modal-overlay"><div class="modal-dialog" id="modal-dialog"></div></div>
                """)
            else:
                response = self.client.open(path, method=request.method, data=request.post_data, content_type="application/json")
                route.fulfill(status=response.status_code, content_type="application/json", body=response.data)

        page.route("https://hub-config.test/**", handle)
        page.goto("https://hub-config.test/")
        page.add_style_tag(path=str(ROOT / "frontend/css/style.css"))
        for script in (
            "utils/modal.js", "utils/hub_config_transfer.js", "api.js",
            f"components/channels_{key.replace('-', '_')}.js",
        ):
            page.add_script_tag(path=str(ROOT / "frontend/js" / script))
        page.evaluate("""() => {
            window.__messages = [];
            window.__copied = null;
            window.__copyCalls = 0;
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async text => { window.__copied = text; window.__copyCalls++; }
            }});
            window.Toast = Object.fromEntries(['success', 'warning', 'error'].map(
                type => [type, message => window.__messages.push({type, message})]
            ));
            Modal.init();
        }""")
        page.evaluate(f"async () => {{ document.getElementById('app').innerHTML = {PAGES[key]}.render(); await {PAGES[key]}.init(); }}")
        return page

    def test_export_copies_saved_json_and_pasted_import_restores_each_module(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                page = self.page_for(key)
                field = "pinchuang-db-host" if key == "pinchuang" else "creative-radar-api-endpoint"
                page.locator(f"#{field}").fill("https://unsaved.example.invalid")
                page.locator(f"#btn-{key}-export-config").click()
                page.wait_for_function("window.__copied !== null")
                text = page.evaluate("window.__copied")
                backup = json.loads(text)
                self.assertEqual(backup["config"], hub.config)
                self.assertTrue(page.locator(f"#btn-{key}-export-config").is_enabled())
                self.assertEqual(page.locator(f"#{field}").input_value(), "https://unsaved.example.invalid")
                self.assertFalse(page.locator("#modal-overlay").is_visible())
                self.assertIn("已复制到剪贴板", page.evaluate("window.__messages.at(-1).message"))

                hub.save_config({"schedule": {"enabled": False, "times": []}})
                page.locator(f"#btn-{key}-import-config").click()
                page.locator("#hub-config-import-json").fill("\ufeff" + text)
                page.locator("#hub-config-import-submit").click()
                page.wait_for_function("!Modal.overlay.classList.contains('active')")
                expected_value = backup["config"]["database"]["host"] if key == "pinchuang" else backup["config"]["api"]["endpoint"]
                page.wait_for_function("([id, value]) => document.getElementById(id).value === value", arg=[field, expected_value])
                self.assertEqual(hub.config, backup["config"])
                self.assertTrue(page.locator(f"#{key}-schedule-enabled").is_checked())
                self.assertIn("18:30", page.locator(f"#{key}-time-list").inner_text())
                secret_input = "pinchuang-db-password" if key == "pinchuang" else "creative-radar-api-key"
                self.assertEqual(page.locator(f"#{secret_input}").input_value(), "")
                self.assertIn("已保存", page.locator(f"#{secret_input}").get_attribute("placeholder"))
                self.assertEqual(page.locator("#hub-config-import-json").input_value(), "")
                self.assertIsNone(hub.worker)
                page.close()

    def test_invalid_paste_can_be_corrected_and_cancel_does_not_save(self):
        for key, hub in self.hubs.items():
            with self.subTest(module=key):
                page = self.page_for(key)
                before = hub.config_path.read_bytes()
                other = next(other for other_key, other in self.hubs.items() if other_key != key)
                page.locator(f"#btn-{key}-import-config").click()
                for value in ("", "{invalid", json.dumps(other.export_config_backup())):
                    page.locator("#hub-config-import-json").fill(value)
                    page.locator("#hub-config-import-submit").click()
                    page.wait_for_function("document.getElementById('hub-config-import-error').textContent.length > 0")
                    self.assertTrue(page.locator("#modal-overlay").is_visible())
                    self.assertEqual(page.locator("#hub-config-import-json").input_value(), value)
                    self.assertEqual(hub.config_path.read_bytes(), before)
                    self.assertTrue(page.locator("#hub-config-import-submit").is_enabled())
                page.locator("#hub-config-import-json").fill(json.dumps(hub.export_config_backup()))
                page.locator("#hub-config-import-submit").click()
                page.wait_for_function("!Modal.overlay.classList.contains('active')")
                before = hub.config_path.read_bytes()
                page.locator(f"#btn-{key}-import-config").click()
                page.locator("#hub-config-import-json").fill('{"unfinished": true}')
                page.locator("#modal-dialog").get_by_role("button", name="取消", exact=True).click()
                self.assertEqual(hub.config_path.read_bytes(), before)
                self.assertEqual(page.locator("#hub-config-import-json").input_value(), "")
                page.close()

    def test_clipboard_fallback_and_retry_do_not_report_false_success(self):
        page = self.page_for("pinchuang")
        page.evaluate("""() => {
            navigator.clipboard.writeText = async () => { throw new Error('denied'); };
            document.execCommand = command => {
                window.__copied = document.activeElement.value;
                return command === 'copy';
            };
        }""")
        page.locator("#btn-pinchuang-export-config").click()
        page.wait_for_function("window.__copied !== null")
        self.assertEqual(json.loads(page.evaluate("window.__copied"))["config"], self.hubs["pinchuang"].config)

        page.evaluate("""() => { document.execCommand = () => false; window.__messages = []; }""")
        self.hubs["pinchuang"].save_config({"database": {"password": '</textarea><img src=x onerror="window.__injected=true">'}})
        page.locator("#btn-pinchuang-export-config").click()
        page.locator("#hub-config-export-json").wait_for(state="visible")
        self.assertEqual(page.evaluate("window.__messages.length"), 0)
        self.assertFalse(page.evaluate("Boolean(window.__injected)"))
        self.assertEqual(json.loads(page.locator("#hub-config-export-json").input_value())["config"], self.hubs["pinchuang"].config)
        page.evaluate("document.execCommand = () => true")
        page.locator("#hub-config-copy").click()
        page.wait_for_function("!Modal.overlay.classList.contains('active')")
        self.assertIn("已复制到剪贴板", page.evaluate("window.__messages.at(-1).message"))
        self.assertEqual(page.locator("#hub-config-export-json").input_value(), "")

    def test_export_server_failure_restores_button_and_does_not_copy(self):
        page = self.page_for("creative-radar")
        page.route("**/api/creative-radar/config/export", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error":"export unavailable"}',
        ))
        page.locator("#btn-creative-radar-export-config").click()
        page.wait_for_function("window.__messages.some(item => item.type === 'error')")
        self.assertIsNone(page.evaluate("window.__copied"))
        self.assertTrue(page.locator("#btn-creative-radar-export-config").is_enabled())
