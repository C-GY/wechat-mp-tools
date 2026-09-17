"""Real proxy lifecycle with OS proxy/trust changes replaced at their boundary."""
import socket
import logging
import time
import asyncio
import http.client
import threading
from unittest.mock import Mock

import pytest

from backend import mitm_proxy


@pytest.fixture
def manager(tmp_path, monkeypatch):
    proxy = mitm_proxy.ProxyManager()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        proxy.port = reserved.getsockname()[1]
    monkeypatch.setattr(mitm_proxy, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(mitm_proxy, 'ensure_ca_certificates', lambda: None)
    monkeypatch.setattr(mitm_proxy, 'install_system_cert', lambda *_: True)
    monkeypatch.setattr(mitm_proxy, 'prepare_mitm_confdir', lambda: tmp_path / 'mitm')
    monkeypatch.setattr(mitm_proxy, '_set_no_proxy', Mock())
    monkeypatch.setattr(mitm_proxy, '_restore_no_proxy', Mock())
    switch = Mock()
    monkeypatch.setattr(mitm_proxy, 'set_system_proxy', switch)
    yield proxy, switch
    proxy.stop()
    if proxy.thread:
        proxy.thread.join(5)
    mitm_proxy.cleanup_mitmproxy_logging_handlers()


def wait_for_master(proxy):
    deadline = time.monotonic() + 5
    while proxy.master is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert proxy.master is not None


def test_bind_failure_does_not_enable_dead_system_proxy(manager):
    proxy, switch = manager
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', proxy.port))
        occupied.listen()
        with pytest.raises(RuntimeError):
            proxy.start()
    assert not proxy.running
    assert not any(call.args[0] is True for call in switch.call_args_list)


def test_start_returns_only_after_real_listener_is_ready(manager):
    proxy, switch = manager
    def enable(enabled, **_):
        if enabled:
            with socket.create_connection(('127.0.0.1', proxy.port), timeout=.5):
                pass
    switch.side_effect = enable
    assert proxy.start()
    assert proxy.running


def test_worker_exit_clears_status_and_system_proxy(manager):
    proxy, switch = manager
    proxy.start()
    wait_for_master(proxy)
    proxy.master.shutdown()
    proxy.thread.join(5)
    assert not proxy.thread.is_alive()
    assert not proxy.running, 'dead worker must not leave the UI showing listening'
    assert switch.call_args.args == (False,), 'system proxy must be disabled after worker exit'


def test_stop_then_restart_accepts_connections(manager):
    proxy, _ = manager
    for _ in range(2):
        assert proxy.start()
        with socket.create_connection(('127.0.0.1', proxy.port), timeout=.5):
            pass
        proxy.stop()
        assert not proxy.running
        assert proxy.last_error == ""
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', proxy.port), timeout=.2)


def test_bind_error_is_preserved_in_status_and_file(manager):
    proxy, _ = manager
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', proxy.port))
        occupied.listen()
        with pytest.raises(RuntimeError):
            proxy.start()
    assert str(proxy.port) in proxy.last_error
    text = (mitm_proxy.DATA_DIR / 'proxy_runtime.log').read_text(encoding='utf-8')
    assert str(proxy.port) in text and 'Listener worker failed' in text
    assert 'SystemExit' in text


def test_untrusted_certificate_does_not_enable_proxy(manager, monkeypatch):
    proxy, switch = manager
    monkeypatch.setattr(mitm_proxy, 'install_system_cert', lambda *_: False)
    with pytest.raises(RuntimeError, match='CA 证书'):
        proxy.start()
    switch.assert_not_called()
    assert not proxy.running and proxy.thread is None


def test_failed_system_proxy_enable_rolls_back_and_stops_worker(manager):
    proxy, switch = manager
    def enable(enabled, **_):
        if enabled:
            raise OSError('registry permission denied')
    switch.side_effect = enable
    with pytest.raises(RuntimeError, match='registry permission denied'):
        proxy.start()
    assert not proxy.running and proxy.thread is None
    assert switch.call_args.args == (False,)
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', proxy.port), timeout=.2)


def test_runtime_warning_is_persisted_and_redacted(manager):
    proxy, _ = manager
    proxy.start()
    logging.getLogger('mitmproxy.proxy.layers.tls').warning(
        'TLS failed for https://channels.weixin.qq.com/path?token=private-value'
    )
    text = (mitm_proxy.DATA_DIR / 'proxy_runtime.log').read_text(encoding='utf-8')
    assert 'TLS failed' in text
    assert 'private-value' not in text
    assert '<REDACTED>' in text


def test_api_status_reports_unexpected_worker_exit(manager, monkeypatch):
    from flask import Flask
    from backend import channels
    proxy, _ = manager
    monkeypatch.setattr(mitm_proxy.ProxyManager, 'get_instance', lambda: proxy)
    monkeypatch.setattr(mitm_proxy, 'check_cert_trusted', lambda: True)
    app = Flask(__name__)
    app.register_blueprint(channels.channels_bp)
    proxy.start()
    proxy.master.shutdown()
    proxy.thread.join(5)
    status = app.test_client().get('/api/channels/proxy/status').get_json()
    assert status['proxy_running'] is False
    assert '意外退出' in status['last_error']
    assert status['log_path'].endswith('proxy_runtime.log')


def local_request(proxy, path, timeout=.5):
    connection = http.client.HTTPConnection('127.0.0.1', proxy.port, timeout=timeout)
    try:
        connection.request('GET', 'http://channels.weixin.qq.com' + path,
                           headers={'Connection': 'close'})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def lose_listener(proxy):
    """Model accept() failing while mitmproxy's master/worker stays alive."""
    async def close_listener():
        await proxy.master.addons.get('proxyserver').servers.update([])
    asyncio.run_coroutine_threadsafe(close_listener(), proxy.loop).result(5)
    assert proxy.running and proxy.thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', proxy.port), timeout=.2)


def test_health_probe_requires_own_http_response_and_never_fakes_page_readiness(manager):
    proxy, _ = manager
    proxy.start()
    upstream_connections = []

    class RejectUpstream:
        def server_connect(self, data):
            upstream_connections.append(data.server.address)
            data.server.error = 'health checks must stay local'

    async def install_guard():
        proxy.master.addons.add(RejectUpstream())
    asyncio.run_coroutine_threadsafe(install_guard(), proxy.loop).result(5)
    assert proxy.is_healthy()
    assert not upstream_connections
    assert mitm_proxy.wait_for_channels_page(0, timeout=0) is None
    old_thread = proxy.thread
    assert proxy.recover_if_unhealthy() is False
    assert proxy.thread is old_thread
    proxy.stop()
    assert not proxy.is_healthy()


@pytest.mark.parametrize('response', [None, b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'])
def test_accepting_tcp_without_own_http_response_is_unhealthy(manager, response):
    proxy, _ = manager
    finished = threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', proxy.port))
        listener.listen()

        def serve():
            with listener.accept()[0] as connection:
                connection.recv(4096)
                if response:
                    connection.sendall(response)
                finished.wait(2)

        worker = threading.Thread(target=serve)
        worker.start()
        proxy.running, proxy.thread = True, worker
        try:
            assert not proxy.is_healthy(timeout=.1)
        finally:
            finished.set()
            worker.join(2)
            proxy.running, proxy.thread = False, None


def test_recovery_failure_is_throttled_and_retried_after_cooldown(manager, monkeypatch):
    proxy, _ = manager
    clock = [100.0]
    monkeypatch.setattr(mitm_proxy.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(proxy, 'is_healthy', lambda: False)
    restart = Mock(side_effect=RuntimeError('bind failed'))
    monkeypatch.setattr(proxy, 'start', restart)
    with pytest.raises(RuntimeError, match='bind failed'):
        proxy.recover_if_unhealthy()
    for _ in range(3):
        with pytest.raises(RuntimeError, match='自动重试'):
            proxy.recover_if_unhealthy()
    assert restart.call_count == 1
    clock[0] += 61
    with pytest.raises(RuntimeError, match='bind failed'):
        proxy.recover_if_unhealthy()
    assert restart.call_count == 2


def test_concurrent_recovery_restarts_lost_listener_only_once(manager):
    proxy, _ = manager
    proxy.start()
    lose_listener(proxy)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: proxy.recover_if_unhealthy(), range(2)))
    assert sorted(results) == [False, True]
    assert proxy.is_healthy()


@pytest.mark.parametrize('browser_found', [True, False])
def test_lost_listener_recovers_existing_page_without_reopening_wechat(manager, monkeypatch, tmp_path, browser_found):
    from backend import wechat_automation
    proxy, _ = manager
    monkeypatch.setattr(mitm_proxy.ProxyManager, 'get_instance', lambda: proxy)
    monkeypatch.setattr(wechat_automation, 'app_dir', lambda: tmp_path)
    monkeypatch.setattr(wechat_automation, 'find_wechat_browser_windows',
                        lambda: [{'hwnd': 42, 'minimized': False}] if browser_found else [])
    open_wechat, reload_browser = Mock(), Mock(return_value=True)
    monkeypatch.setattr(wechat_automation, 'open_wechat_video_channels', open_wechat)
    monkeypatch.setattr(wechat_automation, '_reload_wechat_browser_window', reload_browser)
    proxy.start()
    old_thread = proxy.thread
    # The page was previously collecting; the recorded heartbeat is now stale.
    with monkeypatch.context() as stale:
        stale.setattr(mitm_proxy.time, 'time', lambda: 100.0)
        mitm_proxy.record_channels_page('already-open-page')
    lose_listener(proxy)
    stopped = threading.Event()

    def existing_page_poll():
        while not stopped.wait(.02):
            try:
                local_request(proxy, '/__wx_channels_api/refresh-command?'
                              'page_id=already-open-page&api_ready=1&busy=1')
            except (OSError, http.client.HTTPException):
                pass

    page = threading.Thread(target=existing_page_poll)
    page.start()
    try:
        result = wechat_automation.ensure_wechat_channels_available(
            detection_timeout=.05, open_timeout=.5, recover_browser=True)
    finally:
        stopped.set()
        page.join(2)
    assert result['monitoring_active'], 'lost listener must recover without human action'
    assert result['proxy_restarted']
    assert proxy.thread is not old_thread and proxy.running
    assert not old_thread.is_alive()
    open_wechat.assert_not_called()
    reload_browser.assert_not_called()
