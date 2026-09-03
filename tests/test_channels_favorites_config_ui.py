import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from test_channels_favorites_config import FavoritesConfigFixture


ROOT = Path(__file__).resolve().parents[1]


class FavoritesConfigUiTests(FavoritesConfigFixture):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        super().setUp()
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.opened_paths = []

        def handle(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/":
                route.fulfill(content_type="text/html", body="<main id='app'></main>")
            elif path == "/api/channels/open-parent":
                self.opened_paths.append(json.loads(request.post_data)["path"])
                route.fulfill(content_type="application/json", body="{}")
            else:
                response = self.client.open(
                    path, method=request.method, data=request.post_data,
                    content_type="application/json",
                )
                route.fulfill(status=response.status_code, content_type="application/json", body=response.data)

        self.page.route("http://favorites.test/**", handle)
        self.page.goto("http://favorites.test/")
        self.page.add_script_tag(path=str(ROOT / "frontend/js/api.js"))
        self.page.add_script_tag(path=str(ROOT / "frontend/js/components/channels_user.js"))
        self.page.evaluate("""async () => {
            window.__messages = [];
            window.__navigations = [];
            window.Toast = Object.fromEntries(['success', 'warning', 'error'].map(
                type => [type, message => window.__messages.push({type, message})]
            ));
            window.Router = {navigate(url) { window.__navigations.push(url); }};
            document.getElementById('app').innerHTML = ChannelsUserPage.render();
            await ChannelsUserPage.init();
        }""")

    def upload(self, content):
        self.page.locator("#favorites-config-file").set_input_files({
            "name": "创作者备份.json", "mimeType": "application/json", "buffer": content,
        })
        self.page.wait_for_function("!ChannelsUserPage.isImportingFavoritesConfig")

    def test_export_file_can_restore_an_empty_list_and_be_imported_again(self):
        self.page.locator("#btn-export-favorites-config").click()
        self.page.wait_for_function("window.__messages.some(item => item.type === 'success')")
        self.assertEqual(len(self.opened_paths), 1)
        backup = Path(self.opened_paths[0]).read_bytes()
        self.assertEqual(json.loads(backup)["favorites"], self.authors)

        self.write_favorites([])
        self.page.evaluate("ChannelsUserPage.loadSelectorFavorites()")
        self.assertTrue(self.page.locator("#btn-export-favorites-config").is_disabled())
        self.assertTrue(self.page.locator("#btn-import-favorites-config").is_enabled())
        self.assertTrue(self.page.locator("#selector-favorites-empty").is_visible())

        with self.page.expect_file_chooser() as chooser:
            self.page.locator("#btn-import-favorites-config").click()
        chooser.value.set_files({
            "name": "创作者备份.json", "mimeType": "application/json",
            "buffer": b"\xef\xbb\xbf" + backup,
        })
        self.page.wait_for_function("!ChannelsUserPage.isImportingFavoritesConfig")
        self.assertEqual(self.read_favorites(), self.authors)
        self.assertEqual(self.page.locator("#selector-favorites-grid .favorite-card").count(), 2)
        self.assertTrue(self.page.locator("#btn-export-favorites-config").is_enabled())
        self.assertFalse(self.page.locator("#selector-favorites-empty").is_visible())
        self.assertEqual(self.page.locator("#favorites-config-file").input_value(), "")

        self.upload(backup)
        self.assertEqual(self.read_favorites(), self.authors)
        self.assertIn("新增 0 个，已存在 2 个", self.page.evaluate("window.__messages.at(-1).message"))

    def test_bad_files_leave_favorites_and_buttons_available_for_retry(self):
        original = self.favorites_file.read_bytes()
        for data in (b"not json", b'{"format": "wrong"}'):
            self.upload(data)
            self.assertEqual(self.favorites_file.read_bytes(), original)
            self.assertEqual(self.page.evaluate("window.__messages.at(-1).type"), "error")
            self.assertTrue(self.page.locator("#btn-import-favorites-config").is_enabled())
            self.assertEqual(self.page.locator("#favorites-config-file").input_value(), "")
        self.upload(json.dumps([{"username": "restored", "nickname": "恢复作者"}]).encode())
        self.assertEqual(self.page.locator("#selector-favorites-grid .favorite-card").count(), 3)
        self.assertEqual(self.page.evaluate("window.__messages.at(-1).type"), "success")

    def test_imported_quotes_are_displayed_as_data_and_navigation_encodes_author_id(self):
        author = {
            "username": "作者'&\"?id", "nickname": '<img src=x onerror="window.__injected=true">',
            "head_img_url": '',
        }
        self.upload(json.dumps([author]).encode())
        card = self.page.locator("#selector-favorites-grid .favorite-card").last
        self.assertEqual(card.locator("h4").inner_text(), author["nickname"])
        self.assertFalse(self.page.evaluate("Boolean(window.__injected)"))
        card.click()
        actual = self.page.evaluate("decodeURIComponent(window.__navigations.at(-1).split('username=')[1])")
        self.assertEqual(actual, author["username"])
