"""Browser -> real local Flask API -> durable journal -> compensation pipeline."""
from pathlib import Path
import threading
import time
from unittest.mock import MagicMock

from flask import Flask, request
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from backend import competitor_monitor as monitor, pinchuang


ROOT = Path(__file__).resolve().parents[1]


def test_paged_failures_and_retry_button_run_only_failed_videos(tmp_path, monkeypatch):
    hub = monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")
    author = {"username": "author'quoted", "nickname": "测试作者"}
    hub.config["schedule"]["creator_interval_seconds"] = 0
    hub.state["current_run"] = {"run_id": "original-run", "sync_batch_id": "original-batch", "status": "partial",
        "phase": "finished", "creator_plan": [author], "creators": [], "total_creators": 1,
        "failed_creators": 1, "failed_items": 105, "started_at": "2026-09-14 10:00", "message": "部分作品失败"}
    hub._creator_result("original-run", {"author_id": author["username"], "author_name": author["nickname"],
        "status": "partial", "failed_items": 105, "failures": [{"video_id": str(i), "error_type": "ChunkedEncodingError",
            "stage": "download", "download_attempts": 4, "error": "<script>window.__bad = true</script> 下载中断"} for i in range(105)]})
    monkeypatch.setattr(monitor, "competitor_monitor_hub", hub)
    monkeypatch.setattr(hub, "_validate_database_config", lambda *_: None)
    monkeypatch.setattr(pinchuang, "get_oss_config", lambda **_: {"configured": True})
    monkeypatch.setattr(hub, "_wait_for_wechat", lambda *_: None)
    monkeypatch.setattr(hub, "_refresh_author", lambda *_: 106)
    videos = [{"id": key, "collected_at": time.time() + 1000, "rpa_payload": {"likeCount": 1}}
              for key in [*(str(i) for i in range(105)), "already-successful"]]
    monkeypatch.setattr(hub, "_load_author_videos", lambda *_: videos)
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    monkeypatch.setattr(hub, "_database_adapter", lambda *_: adapter)
    uploaded = []
    def upload(run_id, author, selected):
        uploaded.extend(v["id"] for v in selected)
        hub._uploaded_video_urls = {v["id"]: f"https://fixture.invalid/{v['id']}.mp4" for v in selected}
        return len(selected), []
    monkeypatch.setattr(hub, "_sync_new_videos_to_oss", upload)

    app = Flask(__name__, static_folder=str(ROOT / "frontend"), static_url_path="/static")
    app.register_blueprint(monitor.competitor_monitor_bp)
    requests = []
    @app.before_request
    def record_request():
        if request.path.startswith('/api/'):
            requests.append((request.method, request.full_path))
    @app.get('/')
    def index():
        return '''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/css/style.css">
<main id="monitor-test-root" style="padding:24px;max-width:1400px;margin:auto"></main>
<script>window.Toast={success:()=>{},error:()=>{},warning:()=>{}};</script>
<script src="/static/js/api.js"></script><script src="/static/js/components/channels_competitor_monitor.js"></script>
<script>document.getElementById('monitor-test-root').innerHTML=ChannelsCompetitorMonitorPage.render();ChannelsCompetitorMonitorPage.loadStatus();</script>'''
    server = make_server('127.0.0.1', 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
            try:
                page.goto(base)
                page.locator('#competitor_monitor-creator-rows button').click()
                panel = page.locator('#competitor_monitor-failures-panel')
                expect(panel.locator('tbody tr')).to_have_count(50)
                assert page.evaluate('window.__bad') is None
                panel.get_by_role('button', name='下一页').click()
                expect(panel.locator('tbody tr').first.locator('td').first).to_have_text('50')
                panel.get_by_role('button', name='下一页').click()
                expect(panel.locator('tbody tr')).to_have_count(5)
                expect(panel.get_by_role('button', name='下一页')).to_be_disabled()
                evidence = ROOT / 'scratch' / 'competitor-diagnosis-2026-09-14' / 'failures-ui.png'
                evidence.parent.mkdir(parents=True, exist_ok=True)
                panel.screenshot(path=str(evidence))
                panel.get_by_role('button', name='关闭').click()
                page.locator('#competitor_monitor-recovery-actions').get_by_role('button', name='仅重试失败项').click()
                expect(page.locator('#competitor_monitor-run-title')).to_contain_text('已完成', timeout=15000)
                assert uploaded == [str(i) for i in range(105)]
                rows = adapter.write_snapshots.call_args.args[0]
                assert len(rows) == 105
                assert {row['sync_batch_id'] for row in rows} == {'original-batch'}
                assert sum(method == 'POST' and path.startswith('/api/competitor_monitor/runs/original-run/retry-failed') for method, path in requests) == 1
                assert not errors
            finally:
                browser.close()
    finally:
        hub.stop()
        if hub.worker:
            hub.worker.join(3)
        server.shutdown()
        worker.join(3)
