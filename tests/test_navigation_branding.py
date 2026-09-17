import re
from pathlib import Path

from playwright.sync_api import sync_playwright
from flask import Flask, render_template
from backend.config import APP_TITLE, APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_default_route_and_visual_navigation_order_are_channels_first():
    router = (ROOT / "frontend" / "js" / "router.js").read_text(encoding="utf-8")
    app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "window.location.hash.slice(1) || 'channels_login'" in router
    order_match = re.search(r"const order = \[(.*?)\];", app, re.S)
    assert order_match
    groups = re.findall(r"'([^']+)'", order_match.group(1))
    assert groups == [
        "wechat_channels",
        "douyin",
        "kuaishou",
        "xiaohongshu",
        "bilibili",
        "wechat",
        "common",
    ]


def test_product_ui_uses_new_brand_and_has_no_source_repository_link():
    app = Flask(__name__, template_folder=str(ROOT / "frontend"))
    with app.app_context():
        index = render_template("index.html", app_title=APP_TITLE, app_version=APP_VERSION)
    assert f"<title>{APP_TITLE}</title>" in index
    assert f'<h1>自媒体内容采集工具 <span class="app-version">v{APP_VERSION}</span></h1>' in index
    assert "微信公众号文章下载管理工具" not in index
    assert "Media Tools" not in index
    assert "github.com/" not in index.lower()
    assert "GitHub 地址" not in index
    assert 'data-page="channels_oss_config"' in index
    assert 'data-page="channels_oss_progress"' in index


def test_navigation_dom_is_reordered_to_the_requested_sequence():
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    nav = re.search(r"(<nav class=\"sidebar-nav\">.*?</nav>)", index, re.S).group(1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        try:
            page.set_content(nav)
            page.add_script_tag(path=str(ROOT / "frontend" / "js" / "app.js"))
            page.evaluate("App.orderNavigationGroups()")
            titles = page.locator(".nav-group-title > span:first-child").all_text_contents()
            assert titles == [
                "微信视频号",
                "抖音视频",
                "快手视频",
                "小红书",
                "B站视频",
                "微信公众号",
                "公共服务",
            ]
            assert page.locator("#nav-channels_login").evaluate(
                "element => element.classList.contains('active')"
            )
        finally:
            browser.close()
