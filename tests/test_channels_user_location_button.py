import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
CHANNELS_USER_SCRIPT = ROOT / "frontend" / "js" / "components" / "channels_user.js"


class ChannelsUserLocationButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def new_page(self):
        page = self.browser.new_page()
        page.set_content(
            """
            <div id="channels-user-videos-grid"></div>
            <div id="channels-user-videos-empty"></div>
            <button id="btn-batch-download"></button>
            <span id="selected-summary-text"></span>
            <input id="check-all-videos" type="checkbox">
            """
        )
        page.add_script_tag(path=str(CHANNELS_USER_SCRIPT))
        return page

    def test_location_button_preserves_the_exact_windows_path(self):
        page = self.new_page()
        try:
            result = page.evaluate(
                """() => {
                    const expectedPath = String.raw`C:\\Downloads\\channels\\sample video.mp4`;
                    let receivedPath = null;

                    ChannelsUserPage.videos = [{
                        id: 'video-1',
                        description: 'sample video',
                        cover_url: '',
                        createtime: 0,
                    }];
                    ChannelsUserPage.history = [{
                        title: 'sample video',
                        path: expectedPath,
                    }];
                    ChannelsUserPage.openLocalParent = (path) => {
                        receivedPath = path;
                    };

                    ChannelsUserPage.renderVideos();
                    const button = document.querySelector('[data-action="open-local-parent"], [onclick*="openLocalParent"]');
                    button.click();

                    return { expectedPath, receivedPath };
                }"""
            )
            self.assertEqual(result["receivedPath"], result["expectedPath"])
        finally:
            page.close()

    def test_location_action_uses_the_channels_api(self):
        page = self.new_page()
        try:
            result = page.evaluate(
                """async () => {
                    const expectedPath = String.raw`C:\\Downloads\\channels\\sample video.mp4`;
                    let receivedPath = null;
                    window.API = {
                        channels: {
                            openParent: async (path) => { receivedPath = path; },
                        },
                        articles: {
                            openParent: async () => { throw new Error('wrong API'); },
                        },
                    };
                    window.Toast = { success() {}, error() {} };

                    await ChannelsUserPage.openLocalParent(expectedPath);
                    return { expectedPath, receivedPath };
                }"""
            )
            self.assertEqual(result["receivedPath"], result["expectedPath"])
        finally:
            page.close()

    def test_video_card_displays_all_four_interaction_metrics(self):
        page = self.new_page()
        try:
            result = page.evaluate(
                """() => {
                    ChannelsUserPage.videos = [{
                        id: 'video-1',
                        description: 'sample video',
                        cover_url: '',
                        createtime: 1700000000,
                        like_count: 1234,
                        share_count: 56,
                        favorite_count: 78,
                        comment_count: 90,
                    }];
                    ChannelsUserPage.history = [];
                    ChannelsUserPage.renderVideos();
                    const stats = document.querySelector('.channels-video-stats');
                    return stats ? Array.from(stats.children).map(item => ({
                        label: item.title,
                        text: item.innerText,
                    })) : [];
                }"""
            )
            self.assertEqual([item["label"] for item in result], ["点赞", "分享", "喜欢", "评论"])
            self.assertIn("👍", result[0]["text"])
            self.assertIn("78", result[0]["text"])
            self.assertIn("56", result[1]["text"])
            self.assertIn("❤️", result[2]["text"])
            self.assertIn("1,234", result[2]["text"])
            self.assertIn("90", result[3]["text"])
        finally:
            page.close()

    def test_video_duration_uses_minutes_or_hours_as_needed(self):
        page = self.new_page()
        try:
            result = page.evaluate(
                """() => {
                    const renderDuration = (seconds) => {
                        ChannelsUserPage.videos = [{
                            id: 'video-' + seconds,
                            description: 'sample video',
                            cover_url: '',
                            createtime: 1700000000,
                            duration_seconds: seconds,
                        }];
                        ChannelsUserPage.history = [];
                        ChannelsUserPage.renderVideos();
                        return document.querySelector('.channels-video-duration')?.innerText || '';
                    };
                    return {
                        underHour: renderDuration(125),
                        overHour: renderDuration(3725),
                    };
                }"""
            )
            self.assertIn("02:05", result["underHour"])
            self.assertIn("01:02:05", result["overHour"])
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
