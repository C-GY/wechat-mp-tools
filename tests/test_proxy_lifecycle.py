"""Real proxy lifecycle with OS proxy/trust changes replaced at their boundary."""
import socket
import logging
import time
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
