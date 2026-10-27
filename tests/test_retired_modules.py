"""Exercise the real app wiring without starting services or remote connections."""
from pathlib import Path
import runpy
import sys
from types import ModuleType
from unittest.mock import Mock, patch

from flask import Blueprint

from backend import competitor_monitor, pinchuang


def test_retired_hubs_are_not_constructed_registered_or_started(tmp_path):
    assert pinchuang.pinchuang_hub is None
    hub = competitor_monitor.CompetitorMonitorHub(tmp_path / 'config.json', tmp_path / 'state.json')
    modules = {}
    blueprints = {
        'auth': 'auth_bp', 'accounts': 'accounts_bp', 'articles': 'articles_bp',
        'proxy': 'proxy_bp', 'account_pool_api': 'account_pool_bp',
        'douyin': 'douyin_bp', 'douyin_login': 'douyin_login_bp', 'douyin_auth': 'douyin_auth_bp',
        'kuaishou': 'kuaishou_bp', 'kuaishou_auth': 'kuaishou_auth_bp', 'channels': 'channels_bp',
        'transcode': 'transcode_bp', 'xiaohongshu': 'xhs_bp', 'xiaohongshu_login': 'xhs_login_bp',
        'bilibili': 'bilibili_bp', 'bilibili_login': 'bilibili_login_bp', 'oss': 'oss_bp',
    }
    for name, attribute in blueprints.items():
        module = ModuleType('backend.' + name)
        setattr(module, attribute, Blueprint('test_' + name, __name__))
        modules[module.__name__] = module
    modules['backend.oss'].upload_manager = Mock()
    account_pool = ModuleType('backend.account_pool')
    account_pool.migrate_legacy_config = Mock()
    modules[account_pool.__name__] = account_pool
    rss = ModuleType('backend.rss_scheduler')
    rss.rss_scheduler = Mock()
    modules[rss.__name__] = rss
    # Any accidental import of either retired backend fails this app startup.
    modules['backend.guangce'] = None
    modules['backend.creative_radar'] = None
    with patch.dict(sys.modules, modules), \
         patch('backend.runtime.configure_runtime'), patch('threading.Thread'), \
         patch.object(competitor_monitor, 'competitor_monitor_hub', hub), \
         patch.object(hub, 'start') as start, \
         patch.object(pinchuang, 'get_oss_config', return_value={'configured': True}):
        result = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app.py'), run_name='test_app_wiring')
        app = result['app']
        assert not {'pinchuang', 'guangce', 'creative_radar'} & app.blueprints.keys()
        assert 'competitor_monitor' in app.blueprints
        assert not any(str(rule).startswith(('/api/pinchuang/', '/api/guangce/', '/api/creative-radar/')) for rule in app.url_map.iter_rules())
        start.assert_called_once_with()
        rss.rss_scheduler.start.assert_called_once_with()
        client = app.test_client()
        for prefix in ('pinchuang', 'guangce', 'creative-radar'):
            assert client.get(f'/api/{prefix}/config').status_code == 404
            assert client.post(f'/api/{prefix}/runs', json={}).status_code in {404, 405}
        config = client.get('/api/competitor_monitor/config')
        assert config.status_code == 200
        assert config.json['publish_filter'] == competitor_monitor.DEFAULT_PUBLISH_FILTER
        assert client.get('/').status_code == 200
