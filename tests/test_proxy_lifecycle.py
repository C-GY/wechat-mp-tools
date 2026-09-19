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
    old_thread = proxy.thread
    with proxy._lifecycle_lock:
        proxy.master.shutdown()
        old_thread.join(5)
        assert not old_thread.is_alive()
        assert not proxy.running, 'dead worker must not leave the UI showing listening'
        assert switch.call_args.args == (False,), 'system proxy must be disabled after worker exit'
    wait_until(proxy.is_healthy)
    assert proxy.thread is not old_thread


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
    with proxy._lifecycle_lock:
        proxy.master.shutdown()
        proxy.thread.join(5)
        status = app.test_client().get('/api/channels/proxy/status').get_json()
        assert status['proxy_running'] is False
        assert '意外退出' in status['last_error']
        assert status['log_path'].endswith('proxy_runtime.log')
    wait_until(proxy.is_healthy)


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


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'proxy did not recover without a UI or environment-check call'


def test_lost_listener_recovers_without_environment_checks(manager):
    proxy, switch = manager
    proxy.watchdog_interval = .05
    proxy.watchdog_probe_timeout = .1
    proxy.start()
    old_thread = proxy.thread
    switch.reset_mock()
    lose_listener(proxy)
    wait_until(lambda: proxy.thread is not old_thread and proxy.is_healthy())
    assert not old_thread.is_alive()
    assert any(call.args[0] is False for call in switch.call_args_list)
    assert switch.call_args.args == (True,)


def test_dead_proxy_is_disabled_even_during_recovery_cooldown(manager):
    proxy, switch = manager
    proxy.start()
    proxy._last_recovery_attempt = time.monotonic()
    lose_listener(proxy)
    switch.reset_mock()
    with pytest.raises(RuntimeError, match='自动重试'):
        proxy.recover_if_unhealthy()
    assert switch.call_args is not None, 'dead proxy must not strand all browser traffic'
    assert switch.call_args.args == (False,)
    assert not proxy.running


def test_windows_accept_error_recovers_without_environment_checks(manager):
    proxy, switch = manager
    # A fatal accept error must wake supervision immediately, not wait for
    # periodic polling. mitmproxy installs its own exception handler at run().
    proxy.watchdog_interval = 30
    proxy.watchdog_probe_timeout = .1
    proxy.start()
    old_thread = proxy.thread
    loop = proxy.loop
    if not hasattr(loop, '_proactor'):
        pytest.skip('Windows Proactor accept failure')
    fired = threading.Event()

    def inject():
        def fail_once(listener):
            # Remove our override, restoring the class method without keeping
            # a bound-method reference cycle inside the proactor instance.
            del loop._proactor.accept
            failed = loop.create_future()
            failed.set_exception(OSError(64, 'Injected Windows accept failure'))
            fired.set()
            return failed

        loop._proactor.accept = fail_once

    loop.call_soon_threadsafe(inject)
    # Complete the pending accept; the following AcceptEx now fails exactly
    # where the supplied log's proactor_events.loop calls f.result().
    with socket.create_connection(('127.0.0.1', proxy.port), timeout=.5):
        pass
    assert fired.wait(1)
    wait_until(lambda: proxy.thread is not old_thread and proxy.is_healthy())
    assert not old_thread.is_alive()


def test_watchdog_retries_failed_rebuild_with_system_proxy_disabled(manager, monkeypatch):
    proxy, switch = manager
    proxy.watchdog_interval = .03
    proxy.watchdog_probe_timeout = .1
    proxy.recovery_cooldown = .2
    proxy.start()
    start_worker = proxy._start
    failed = threading.Event()
    attempts = []

    def start():
        attempts.append(time.monotonic())
        assert not proxy._proxy_enabled
        if len(attempts) == 1:
            failed.set()
            raise RuntimeError('temporary bind failure')
        return start_worker()

    monkeypatch.setattr(proxy, '_start', start)
    lose_listener(proxy)
    assert failed.wait(2)
    assert switch.call_args.args == (False,)
    assert not proxy.running
    wait_until(proxy.is_healthy)
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= .18
    assert switch.call_args.args == (True,)


def test_manual_stop_cancels_watchdog_retries_and_restart_gets_one_supervisor(manager):
    proxy, switch = manager
    proxy.watchdog_interval = .03
    proxy.recovery_cooldown = .2
    proxy.start()
    old_watchdog = proxy._watchdog_thread
    proxy._last_recovery_attempt = time.monotonic()
    lose_listener(proxy)
    wait_until(lambda: not proxy.running)
    proxy.stop()
    switch.reset_mock()
    time.sleep(.25)
    assert not old_watchdog.is_alive()
    assert not proxy.running and proxy.thread is None
    switch.assert_not_called()
    proxy.start()
    assert proxy._watchdog_thread is not old_watchdog
    watchdog = proxy._watchdog_thread
    proxy.start()
    assert proxy._watchdog_thread is watchdog
    assert proxy.is_healthy()


def test_stalled_event_loop_releases_system_proxy_before_waiting_for_worker(manager):
    proxy, switch = manager
    proxy.watchdog_interval = .03
    proxy.watchdog_probe_timeout = .05
    proxy.start()
    old_thread = proxy.thread
    blocked, release, disabled = threading.Event(), threading.Event(), threading.Event()
    switch.side_effect = lambda enabled, **_: disabled.set() if not enabled else None

    def stall():
        blocked.set()
        release.wait(3)

    proxy.loop.call_soon_threadsafe(stall)
    try:
        assert blocked.wait(1)
        assert disabled.wait(1), 'network must be released even while the event loop is stuck'
        assert old_thread.is_alive()
        assert switch.call_args.args == (False,)
    finally:
        release.set()
    wait_until(lambda: proxy.thread is not old_thread and proxy.is_healthy())


def test_repeated_listener_failures_do_not_leak_workers_or_watchdogs(manager):
    proxy, _ = manager
    proxy.watchdog_interval = .03
    proxy.watchdog_probe_timeout = .1
    proxy.recovery_cooldown = .06
    proxy.start()
    watchdog = proxy._watchdog_thread
    retired = []
    for _ in range(12):
        retired.append(proxy.thread)
        lose_listener(proxy)
        wait_until(lambda: proxy.thread is not retired[-1] and proxy.is_healthy())
        assert proxy._watchdog_thread is watchdog
    assert not any(worker.is_alive() for worker in retired)
    proxy.stop()
    assert not watchdog.is_alive()


def test_http_probe_failure_at_startup_never_redirects_system_traffic(manager, monkeypatch):
    proxy, switch = manager
    monkeypatch.setattr(proxy, '_probe_http_health', lambda **_: False)
    with pytest.raises(RuntimeError, match='HTTP'):
        proxy.start()
    assert not any(call.args[0] is True for call in switch.call_args_list)
    assert not proxy.running and proxy.thread is None


def test_environment_started_proxy_also_gets_continuous_supervision(manager):
    proxy, _ = manager
    proxy.watchdog_interval = .03
    proxy.recovery_cooldown = .05
    assert proxy.recover_if_unhealthy()
    old_thread = proxy.thread
    lose_listener(proxy)
    wait_until(lambda: proxy.thread is not old_thread and proxy.is_healthy())


@pytest.mark.parametrize('https', [False, True])
def test_normal_browser_traffic_works_after_unattended_recovery(manager, tmp_path, https):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ssl

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'ordinary browser page'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    client_context = None
    if https:
        from datetime import datetime, timedelta, timezone
        from ipaddress import ip_address
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / 'test.crt', tmp_path / 'test.key'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        upstream.socket = server_context.wrap_socket(upstream.socket, server_side=True)
        client_context = ssl.create_default_context(cafile=str(cert_path))
    server_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    server_thread.start()
    proxy, _ = manager
    proxy.watchdog_interval = .03
    proxy.recovery_cooldown = .05
    proxy.start()

    def browse():
        if https:
            connection = http.client.HTTPSConnection('127.0.0.1', proxy.port,
                                                     timeout=2, context=client_context)
            connection.set_tunnel('127.0.0.1', upstream.server_port)
            path = '/'
        else:
            connection = http.client.HTTPConnection('127.0.0.1', proxy.port, timeout=1)
            path = f'http://127.0.0.1:{upstream.server_port}/'
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 200
            return response.read() == b'ordinary browser page'
        finally:
            connection.close()

    try:
        assert browse()
        old_thread = proxy.thread
        lose_listener(proxy)
        wait_until(lambda: proxy.thread is not old_thread and proxy.is_healthy())
        assert browse()
    finally:
        upstream.shutdown()
        upstream.server_close()
        server_thread.join(2)


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
    monkeypatch.setattr(proxy, 'is_healthy', lambda **_: False)
    restart = Mock(side_effect=RuntimeError('bind failed'))
    monkeypatch.setattr(proxy, '_start_worker_locked', restart)
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
