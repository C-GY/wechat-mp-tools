"""Real browser API wrapper -> Flask -> isolated config and disposable S3 check."""
from pathlib import Path

from flask import Flask
from playwright.sync_api import sync_playwright

from backend import oss
from test_oss_endpoint import S3Session


def test_domain_form_saves_and_verifies_through_real_api_without_exposing_secret(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    oss.save_oss_config('upload-user', 'SECRET_SENTINEL', 'video-bucket', 'https://oss.fandow.com')
    before = oss.OSS_CONFIG_FILE.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(oss.oss_bp)
    session = S3Session()
    monkeypatch.setattr(oss.requests, 'Session', lambda: session)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1200, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                request = route.request
                path = request.url.removeprefix('https://oss-ui.test')
                if path.startswith('/api/oss/'):
                    response = app.test_client().open(path, method=request.method, data=request.post_data, content_type='application/json')
                    route.fulfill(status=response.status_code, content_type='application/json', body=response.data)
                else:
                    route.fulfill(content_type='text/html', body='<meta charset="utf-8"><main id="oss-test-root" style="padding:24px"></main>')

            page.route('https://oss-ui.test/**', handle)
            page.goto('https://oss-ui.test/')
            page.add_style_tag(path=str(root / 'frontend/css/style.css'))
            page.add_script_tag(path=str(root / 'frontend/js/api.js'))
            page.add_script_tag(path=str(root / 'frontend/js/components/channels_oss_config.js'))
            page.evaluate('''async () => {
                window.Toast = {success() {}, warning() {}, error() {}};
                document.getElementById('oss-test-root').innerHTML = ChannelsOSSConfigPage.render();
                await ChannelsOSSConfigPage.init();
            }''')
            page.locator('#oss-endpoint').fill('minio.fandow.com')
            page.locator('#btn-test-oss-connection').click()
            page.wait_for_function("document.getElementById('oss-config-status').textContent.includes('验证通过')")
            assert oss.OSS_CONFIG_FILE.read_bytes() == before
            assert [entry[0] for entry in session.calls] == ['PUT', 'HEAD', 'GET', 'DELETE']
            assert all(entry[1].startswith('https://minio.fandow.com/video-bucket/') for entry in session.calls)
            page.locator('#btn-save-oss-config').click()
            page.wait_for_function("!document.getElementById('btn-save-oss-config').disabled")
            assert oss.get_oss_config()['endpoint'] == 'https://minio.fandow.com'
            assert oss.get_oss_config(include_secret=True)['access_key_secret'] == 'SECRET_SENTINEL'
            assert page.locator('#oss-access-key-secret').input_value() == ''
            assert 'SECRET_SENTINEL' not in page.locator('body').inner_text()
            evidence = root / 'scratch' / 'oss-config-endpoint.png'
            evidence.parent.mkdir(parents=True, exist_ok=True)
            page.locator('#oss-test-root').screenshot(path=str(evidence))
            assert errors == []
        finally:
            browser.close()
