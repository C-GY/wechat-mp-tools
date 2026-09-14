from pathlib import Path

from playwright.sync_api import sync_playwright


def test_proxy_failure_updates_visible_status_and_stops_polling_on_leave():
    script = Path(__file__).resolve().parents[1] / 'frontend/js/components/channels_login.js'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page()
            page.clock.install()
            page.add_script_tag(path=str(script))
            page.evaluate('''async () => {
                window.statusCalls = 0;
                window.proxyStatus = {proxy_running:true, cert_installed:true};
                window.API = {
                    settings: {get:async () => ({})},
                    channels: {getProxyStatus:async () => {
                        window.statusCalls++;
                        return window.proxyStatus;
                    }}
                };
                document.body.innerHTML = ChannelsLoginPage.render();
                await ChannelsLoginPage.init();
            }''')
            assert page.locator('#proxy-status-badge').inner_text() == '运行中'
            page.evaluate('''() => {window.proxyStatus = {
                proxy_running:false, cert_installed:true,
                last_error:'监听退出 <img src=x onerror=alert(1)>',
                log_path:'C:/app/data/proxy_runtime.log'
            };}''')
            page.clock.run_for(3100)
            assert page.locator('#proxy-status-badge').inner_text() == '监听异常'
            assert '启动同步助手' in page.locator('#btn-toggle-proxy').inner_text()
            assert 'proxy_runtime.log' in page.locator('#proxy-runtime-error').inner_text()
            assert page.locator('#proxy-runtime-error img').count() == 0
            page.evaluate('ChannelsLoginPage.destroy()')
            calls = page.evaluate('window.statusCalls')
            page.clock.run_for(6000)
            assert page.evaluate('window.statusCalls') == calls
        finally:
            browser.close()


def test_cached_login_page_pauses_status_polling_until_shown_again():
    root = Path(__file__).resolve().parents[1] / 'frontend/js'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page()
            page.clock.install()
            page.set_content('<div id="page-container"></div>')
            page.add_script_tag(path=str(root / 'components/channels_login.js'))
            page.add_script_tag(path=str(root / 'router.js'))
            page.evaluate('''async () => {
                window.statusCalls = 0;
                window.API = {
                    settings: {get:async () => ({})},
                    channels: {getProxyStatus:async () => {
                        window.statusCalls++;
                        return {proxy_running:true, cert_installed:true};
                    }}
                };
                Router.routes = {
                    channels_login: ChannelsLoginPage,
                    other: {render:() => '<p>Other page</p>'}
                };
                await Router.handleRouting();
            }''')
            page.clock.run_for(3100)
            calls = page.evaluate('window.statusCalls')
            page.evaluate('''async () => {
                location.hash = '#other';
                await Router.handleRouting();
            }''')
            page.clock.run_for(6100)
            assert page.evaluate('window.statusCalls') == calls
            page.evaluate('''async () => {
                location.hash = '#channels_login';
                await Router.handleRouting();
            }''')
            assert page.evaluate('window.statusCalls') == calls + 1
            page.clock.run_for(3100)
            assert page.evaluate('window.statusCalls') == calls + 2
        finally:
            browser.close()
