from unittest.mock import MagicMock, patch

import pymysql
import pytest
from flask import Flask

from backend import competitor_monitor as monitor
from backend.competitor_author_tags import CompetitorAuthorTagStore, normalize_change


VALID = {"operation": "add", "accounts": [{"platform": "wechat_channels", "author_id": "one"}], "tags": ["关注"]}


@pytest.mark.parametrize("payload", [None, [], {}, {**VALID, "operation": []}, {**VALID, "accounts": []},
    {**VALID, "accounts": [None]}, {**VALID, "accounts": VALID["accounts"] * 501}, {**VALID, "tags": []},
    {**VALID, "tags": "关注"}, {**VALID, "tags": [None]}, {**VALID, "tags": [" "]},
    {**VALID, "tags": ["a" * 129]}, {**VALID, "tags": ["a\x00b"]}, {**VALID, "tags": ["关注"] * 21}])
def test_invalid_batch_does_not_open_database(payload):
    connect = MagicMock()
    with pytest.raises(ValueError):
        CompetitorAuthorTagStore(connect).change_tags(payload)
    connect.assert_not_called()


def test_normalization_keeps_account_pairs_and_deduplicates_tags():
    operation, accounts, tags = normalize_change({**VALID, "accounts": VALID["accounts"] * 2 + [
        {"platform": "other", "author_id": "one"}], "tags": [" 关注 ", "关注", "有 空格"]})
    assert operation == "add"
    assert accounts == [("other", "one"), ("wechat_channels", "one")]
    assert tags == ["关注", "有 空格"]


def test_api_returns_safe_errors_and_passes_explicit_account_selection():
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    client = app.test_client()
    store = MagicMock()
    store.list_accounts.return_value = [{"author_id": "one", "tags": ["关注"]}]
    store.change_tags.return_value = {"accounts": 1, "changed": 1, "tags": 1, "operation": "add"}
    with patch.object(monitor, "_author_tag_store", return_value=store):
        result = client.get('/api/competitor_monitor/authors')
        assert result.json['items'][0]['author_id'] == 'one'
        assert result.headers['Cache-Control'] == 'no-store'
        assert client.post('/api/competitor_monitor/author-tags/batch', json=VALID).json['changed'] == 1
        store.change_tags.assert_called_once_with(VALID)
        store.change_tags.side_effect = ValueError('标签不能为空')
        assert client.post('/api/competitor_monitor/author-tags/batch', json={}).status_code == 400
        store.change_tags.side_effect = pymysql.OperationalError(1045, 'sensitive database connection detail')
        response = client.post('/api/competitor_monitor/author-tags/batch', json=VALID)
        assert response.status_code == 503
        assert 'sensitive' not in response.text
        store.list_accounts.side_effect = store.change_tags.side_effect
        assert client.get('/api/competitor_monitor/authors').status_code == 503
