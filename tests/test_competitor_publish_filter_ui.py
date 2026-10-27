"""Real browser form -> isolated Flask config API; no production app or DB."""
from datetime import datetime, timezone
import json
from pathlib import Path

from flask import Flask
from playwright.sync_api import sync_playwright

from backend import competitor_monitor as monitor, pinchuang


ROOT = Path(__file__).resolve().parents[1]


def test_publication_filter_form_preview_persistence_and_run_scope(tmp_path, monkeypatch):
    hub = monitor.CompetitorMonitorHub(tmp_path / 'config.json', tmp_path / 'state.json')
    monkeypatch.setattr(monitor, 'competitor_monitor_hub', hub)
    monkeypatch.setattr(pinchuang, 'get_oss_config', lambda: {'configured': True})
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    saves = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1200, 'height': 950})
            page.clock.install(time=datetime(2026, 10, 19, 2, tzinfo=timezone.utc))
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                request = route.request
                endpoint = request.url.removeprefix('https://publish-filter.test')
                if endpoint.startswith('/api/competitor_monitor/'):
                    if request.method == 'POST':
                        saves.append(json.loads(request.post_data))
                    response = app.test_client().open(endpoint, method=request.method,
                        data=request.post_data, content_type='application/json')
                    route.fulfill(status=response.status_code, content_type='application/json', body=response.data)
                else:
                    route.fulfill(content_type='text/html', body='<meta charset="utf-8"><main id="monitor-test-root" style="padding:24px"></main>')

            page.route('https://publish-filter.test/**', handle)
            page.goto('https://publish-filter.test/')
            page.add_style_tag(path=str(ROOT / 'frontend/css/style.css'))
            page.add_script_tag(path=str(ROOT / 'frontend/js/api.js'))
            page.add_script_tag(path=str(ROOT / 'frontend/js/components/channels_competitor_monitor.js'))
            page.evaluate('''async () => {
                window.__messages = [];
                window.Toast = {success: text => __messages.push(text), error: text => __messages.push(text), warning: text => __messages.push(text)};
                document.getElementById('monitor-test-root').innerHTML = ChannelsCompetitorMonitorPage.render();
                await ChannelsCompetitorMonitorPage.loadConfig();
            }''')
            select = page.locator('#competitor_monitor-publish-mode')
            date_input = page.locator('#competitor_monitor-publish-before')
            button = page.get_by_role('button', name='保存发布时间配置', exact=True)
            assert select.input_value() == 'all'
            assert not date_input.is_visible()
            for mode, expected in [('year', '2025-10-19'), ('half_year', '2026-04-19'),
                                   ('three_months', '2026-07-19'), ('month', '2026-09-19'), ('week', '2026-10-12')]:
                select.select_option(mode)
                assert date_input.input_value() == expected
                assert date_input.get_attribute('readonly') is not None
                assert expected in page.locator('#competitor_monitor-publish-hint').inner_text()
            for value, mode, expected in [('2026-03-31T02:00:00Z', 'month', '2026-02-28'),
                                          ('2024-02-29T02:00:00Z', 'year', '2023-02-28'),
                                          ('2026-10-31T17:00:00Z', 'month', '2026-10-01')]:
                assert page.evaluate('([mode, value]) => ChannelsCompetitorMonitorPage.publishCutoff(mode, new Date(value))', [mode, value]) == expected
            button.click()
            page.wait_for_function("__messages.includes('竞对监测配置已保存') && !document.getElementById('btn-competitor_monitor-save-publish').disabled")
            assert hub.config['publish_filter'] == {'mode': 'week', 'before_date': ''}
            select.select_option('custom')
            date_input.fill('2026-09-01')
            button.click()
            page.wait_for_function("!document.getElementById('btn-competitor_monitor-save-publish').disabled")
            assert hub.config['publish_filter'] == {'mode': 'custom', 'before_date': '2026-09-01'}
            page.evaluate('''async () => {
                document.getElementById('monitor-test-root').innerHTML = ChannelsCompetitorMonitorPage.render();
                await ChannelsCompetitorMonitorPage.loadConfig();
                ChannelsCompetitorMonitorPage.renderStatus({current_run: {run_id:'saved-run', status:'paused', publish_filter:{mode:'month',before_date:'2026-08-19'}}});
            }''')
            assert date_input.input_value() == '2026-09-01'
            assert '2026-08-19' in page.locator('#competitor_monitor-run-publish-filter').inner_text()
            card = page.locator('.card').filter(has=page.get_by_role('heading', name='📅 发布时间截止配置'))
            evidence = ROOT / 'scratch' / 'competitor-publish-filter.png'
            evidence.parent.mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(evidence))
            date_input.fill('')
            button.click()
            page.wait_for_function("__messages.includes('请选择发布时间截止日期')")
            assert len(saves) == 2
            assert hub.config['publish_filter']['before_date'] == '2026-09-01'
            assert errors == []
        finally:
            browser.close()
