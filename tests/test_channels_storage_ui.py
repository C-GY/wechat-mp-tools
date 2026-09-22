"""Real browser -> Flask -> migrated SQLite -> backup/export verification."""
import json
from pathlib import Path
import threading

from flask import Flask
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend import channels
from backend.channels_storage import FeedStore, feed_store


def test_storage_panel_migrates_and_creates_readable_backup_and_export(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path/'feeds.json'
    original = {'author':[{'id':'1','description':'历史作品','oss_video_url':'retained'}]}
    source.write_text(json.dumps(original),encoding='utf-8')
    monkeypatch.setattr(channels,'CHANNELS_FEEDS_FILE',source)
    app = Flask(__name__,static_folder=str(root/'frontend'),static_url_path='/static')
    app.register_blueprint(channels.channels_bp)
    @app.get('/')
    def index():
        return '''<meta charset="utf-8"><link rel="stylesheet" href="/static/css/style.css">
        <div id="app"><main class="main-content"><div class="page-container"><div id="test-page" class="route-page"></div></div></main></div>
        <script src="/static/js/api.js"></script>
        <script src="/static/js/components/channels_accounts.js"></script>
        <script>const Toast={success(){},error(){}};
        document.getElementById('test-page').innerHTML=ChannelsAccountsPage.render();
        ChannelsAccountsPage.refreshStorage(true);</script>'''
    server = make_server('127.0.0.1',0,app,threaded=True)
    worker = threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge',headless=True)
            page = browser.new_page(viewport={'width':1200,'height':950})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.wait_for_function("document.querySelector('#channels-storage-status').textContent.includes('已就绪')")
            store=feed_store(source)
            assert store.author('author')==original['author']
            page.get_by_role('button',name='备份作品库',exact=True).click()
            page.wait_for_function("document.querySelector('#channels-storage-detail').textContent.includes('.sqlite3')")
            backup=Path(store.status()['maintenance']['path'])
            assert FeedStore(tmp_path/'absent.json',backup).author('author')==original['author']
            store.merge('author',[{'id':'2','description':'新增作品'}])
            page.get_by_role('button',name='导出完整 JSON',exact=True).click()
            page.wait_for_function("document.querySelector('#channels-storage-detail').textContent.includes('.json')")
            exported=Path(store.status()['maintenance']['path'])
            assert len(json.loads(exported.read_text(encoding='utf-8'))['author'])==2
            assert json.loads(source.read_text(encoding='utf-8'))==original
            assert not errors
            (root/'scratch').mkdir(exist_ok=True)
            page.screenshot(path=str(root/'scratch/channels-storage-ui.png'),full_page=True)
            browser.close()
    finally:
        server.shutdown()
        worker.join(5)
