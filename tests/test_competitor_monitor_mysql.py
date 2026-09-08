"""Opt-in tests against the configured MySQL tables; every test rolls back its rows.

Run with COMPETITOR_MYSQL_INTEGRATION=1. Credentials stay in the user profile.
"""

import os
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pymysql

from backend.competitor_monitor_store import CompetitorMySQLAdapter, build_row
from backend.competitor_author_tags import CompetitorAuthorTagStore
from backend.oss import persistent_config_dir

pytestmark = pytest.mark.skipif(os.environ.get("COMPETITOR_MYSQL_INTEGRATION") != "1", reason="opt-in real MySQL transaction tests")


@pytest.fixture
def database():
    config_path = Path(os.environ.get("COMPETITOR_MYSQL_CONFIG") or persistent_config_dir() / "competitor_monitor_config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    adapter = CompetitorMySQLAdapter(config["database"])
    connection = adapter.connect()

    class Transaction:
        def cursor(self):
            return connection.cursor()

        def commit(self):
            pass  # Exercise all SQL but leave test data invisible to other sessions.

        def rollback(self):
            connection.rollback()

        def close(self):
            pass

    try:
        with patch.object(adapter, "connect", return_value=Transaction()):
            yield adapter, connection
    finally:
        connection.rollback()
        connection.close()


def make_row(key, batch, timestamp, **changes):
    return build_row({"username": "integration-test", "nickname": "事务回滚验证"},
                     {"id": key, "description": "验证 #测试", "like_count": 10, "share_count": 3,
                      "favorite_count": 0, "comment_count": None, "oss_video_url": "https://example.invalid/transaction-test.mp4", **changes},
                     batch, synced_at=timestamp)


def test_real_schema_is_three_tables(database):
    adapter, _ = database
    assert len(adapter.test_connection()["tables"]) == 3


def test_three_tables_batch_idempotency_null_zero_and_out_of_order(database):
    adapter, connection = database
    key = "transaction-test-" + uuid.uuid4().hex
    t = datetime(2026, 9, 8, 12)
    assert adapter.write_snapshots([make_row(key, "a", t, tags=["测试", "TEST", "test"])]) == 1
    with connection.cursor() as c:
        c.execute("SELECT * FROM competitor_videos WHERE source_video_key=%s", (key,))
        first = c.fetchone()
        c.execute("SELECT * FROM competitor_video_snapshots WHERE video_id=%s", (first["video_id"],))
        first_snapshot = c.fetchone()
    adapter.write_snapshots([make_row(key, "a", t + timedelta(minutes=1), like_count=15)])
    adapter.write_snapshots([make_row(key, "b", t + timedelta(minutes=2), like_count=8, oss_video_url="https://example.invalid/replaced.mp4")])
    adapter.write_snapshots([make_row(key, "old", t - timedelta(minutes=1), like_count=99, tags=["过期标签"])])
    adapter.write_snapshots([make_row(key, "a", t, like_count=100)])
    with connection.cursor() as c:
        c.execute("SELECT * FROM competitor_videos WHERE video_id=%s", (first["video_id"],))
        master = c.fetchone()
        assert master["like_count"] == 8
        assert master["favorite_count"] == 0
        assert master["comment_count"] is None
        assert master["video_url"] == first["video_url"]
        assert master["created_at"] == first["created_at"]
        assert master["last_synced_at"] == t + timedelta(minutes=2)
        c.execute("SELECT * FROM competitor_video_snapshots WHERE video_id=%s", (first["video_id"],))
        snapshots = {r["sync_batch_id"]: r for r in c.fetchall()}
        assert len(snapshots) == 3
        assert snapshots["a"]["like_count"] == 15
        assert snapshots["a"]["snapshot_id"] == first_snapshot["snapshot_id"]
        assert snapshots["a"]["created_at"] == first_snapshot["created_at"]
        assert snapshots["old"]["like_count"] == 99
        c.execute("SELECT tag_name FROM competitor_video_tags WHERE video_id=%s", (first["video_id"],))
        assert {r["tag_name"] for r in c.fetchall()} == {"测试", "TEST"}
    latest = adapter.latest_rows([key])
    assert latest[key]["video_id"] == first["video_id"]


def test_sql_failure_rolls_back_master_tags_and_preceding_rows(database):
    adapter, connection = database
    prefix = "rollback-test-" + uuid.uuid4().hex
    t = datetime(2026, 9, 8, 12)
    good = make_row(prefix + "-a", "a", t)
    invalid = make_row(prefix + "-b", "a", t)
    invalid["sync_batch_id"] = None  # Fail in snapshot INSERT, after the master and tag writes.
    with pytest.raises(Exception):
        adapter.write_snapshots([good, invalid])
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM competitor_videos WHERE source_video_key IN (%s,%s)", (good["source_video_key"], invalid["source_video_key"]))
        assert c.fetchone()["n"] == 0


def test_same_millisecond_retry_does_not_replace_later_snapshot(database):
    adapter, connection = database
    key = "tie-test-" + uuid.uuid4().hex
    t = datetime(2026, 9, 8, 12)
    adapter.write_snapshots([make_row(key, "a", t, like_count=1)])
    adapter.write_snapshots([make_row(key, "b", t, like_count=2)])
    adapter.write_snapshots([make_row(key, "a", t, like_count=3)])
    with connection.cursor() as c:
        c.execute("SELECT like_count FROM competitor_videos WHERE source_video_key=%s", (key,))
        assert c.fetchone()["like_count"] == 2


def test_manual_author_tags_survive_collection_and_author_rename(database):
    adapter, connection = database
    author_id = "manual-author-test-" + uuid.uuid4().hex
    platform = "wechat_channels"
    with connection.cursor() as c:
        c.executemany(
            "INSERT INTO competitor_author_tags (platform,author_id,tag_name) VALUES (%s,%s,%s)",
            [(platform, author_id, "重点关注"), (platform, author_id, "护肤"),
             (platform, author_id + "-other", "重点关注"), ("other_platform", author_id, "重点关注")],
        )
        with pytest.raises(pymysql.IntegrityError):
            c.execute("INSERT INTO competitor_author_tags (platform,author_id,tag_name) VALUES (%s,%s,%s)",
                      (platform, author_id, "重点关注"))
        for values in ((platform, author_id, "   "), (platform, "", "测试"), ("", author_id, "测试")):
            with pytest.raises(pymysql.MySQLError):
                c.execute("INSERT INTO competitor_author_tags (platform,author_id,tag_name) VALUES (%s,%s,%s)", values)
        c.execute("SELECT * FROM competitor_author_tags WHERE author_id IN (%s,%s) ORDER BY author_tag_id",
                  (author_id, author_id + "-other"))
        before = c.fetchall()

    author = {"username": author_id, "nickname": "原账号名称"}
    videos = [{"id": author_id + suffix, "description": "作品 #自动话题", "like_count": 0,
               "oss_video_url": "https://example.invalid/manual-tag-test.mp4"} for suffix in ("-a", "-b")]
    timestamp = datetime(2026, 9, 8, 12)
    adapter.write_snapshots([build_row(author, video, "manual-tags-a", synced_at=timestamp) for video in videos])
    author["nickname"] = "修改后的账号名称"
    adapter.write_snapshots([build_row(author, video, "manual-tags-b", synced_at=timestamp + timedelta(minutes=1))
                            for video in videos])
    with connection.cursor() as c:
        c.execute("SELECT * FROM competitor_author_tags WHERE author_id IN (%s,%s) ORDER BY author_tag_id",
                  (author_id, author_id + "-other"))
        assert c.fetchall() == before  # Collection neither edits manual tags nor imports video topics.
        c.execute("SELECT v.source_video_key,v.author_name FROM competitor_videos v "
                  "JOIN competitor_author_tags t ON t.platform=v.platform AND t.author_id=v.author_id "
                  "WHERE t.platform=%s AND t.author_id=%s AND t.tag_name=%s",
                  (platform, author_id, "重点关注"))
        matched = c.fetchall()
        assert {r["source_video_key"] for r in matched} == {v["id"] for v in videos}
        assert {r["author_name"] for r in matched} == {"修改后的账号名称"}


def test_account_tag_store_batch_add_remove_and_latest_account_listing(database):
    adapter, connection = database
    prefix = 'account-store-test-' + uuid.uuid4().hex
    accounts = [{"platform": "wechat_channels", "author_id": prefix + suffix} for suffix in ('-a', '-b')]
    for account in accounts:
        for i in range(2):
            row = make_row(account['author_id'] + str(i), 'accounts', datetime(2026, 9, 8, 12, i))
            row.update(author_id=account['author_id'], author_name=f'账号名称{i}')
            adapter.write_snapshots([row])
    store = CompetitorAuthorTagStore(adapter.connect)
    payload = {'operation': 'add', 'accounts': accounts, 'tags': ['重点', '原有']}
    assert store.change_tags(payload)['changed'] == 4
    with connection.cursor() as c:
        c.execute('SELECT author_tag_id,created_at,updated_at FROM competitor_author_tags WHERE author_id LIKE %s ORDER BY author_tag_id', (prefix + '%',))
        before = c.fetchall()
    assert store.change_tags(payload)['changed'] == 0
    with connection.cursor() as c:
        c.execute('SELECT author_tag_id,created_at,updated_at FROM competitor_author_tags WHERE author_id LIKE %s ORDER BY author_tag_id', (prefix + '%',))
        assert c.fetchall() == before
        # Manual SQL may use different casing; the list must follow MySQL's identity collation.
        c.execute('INSERT INTO competitor_author_tags(platform,author_id,tag_name) VALUES (%s,%s,%s)',
                  ('wechat_channels', accounts[0]['author_id'].upper(), '大小写'))
        c.execute('INSERT INTO competitor_author_tags(platform,author_id,tag_name) VALUES (%s,%s,%s)',
                  ('other_platform', prefix + '-no-videos', '预先分类'))
    listed = [a for a in store.list_accounts() if a['author_id'].startswith(prefix)]
    assert len(listed) == 3
    first = next(a for a in listed if a['author_id'] == accounts[0]['author_id'])
    assert first['author_name'] == '账号名称1'
    assert first['video_count'] == 2
    assert set(first['tags']) == {'重点', '原有', '大小写'}
    assert next(a for a in listed if a['platform'] == 'other_platform')['video_count'] == 0
    assert store.change_tags({**payload, 'operation': 'remove', 'accounts': accounts[:1], 'tags': ['重点']})['changed'] == 1
    listed = {a['author_id']: a for a in store.list_accounts() if a['author_id'].startswith(prefix)}
    assert set(listed[accounts[0]['author_id']]['tags']) == {'原有', '大小写'}
    assert set(listed[accounts[1]['author_id']]['tags']) == {'原有', '重点'}
    with pytest.raises(ValueError, match='不存在'):
        store.change_tags({**payload, 'accounts': accounts + [{'platform': 'wechat_channels', 'author_id': prefix + '-unknown'}], 'tags': ['不能部分提交']})
    with connection.cursor() as c:
        c.execute('SELECT COUNT(*) AS n FROM competitor_author_tags WHERE author_id LIKE %s AND tag_name=%s', (prefix + '%', '不能部分提交'))
        assert c.fetchone()['n'] == 0


def test_account_batch_sql_failure_rolls_back_prior_tag_writes(database):
    adapter, connection = database
    key = 'account-rollback-test-' + uuid.uuid4().hex
    row = make_row(key, 'account-rollback', datetime(2026, 9, 8, 12))
    row['author_id'] = key
    adapter.write_snapshots([row])
    transaction = adapter.connect()
    inserts = []

    class FaultCursor:
        def __enter__(self):
            self.cursor = connection.cursor()
            return self
        def __exit__(self, *args):
            self.cursor.close()
        def execute(self, sql, params):
            if sql.startswith('INSERT INTO competitor_author_tags'):
                inserts.append(params)
                if len(inserts) == 2:
                    raise pymysql.OperationalError(1205, 'injected failure after a successful insert')
            return self.cursor.execute(sql, params)
        def fetchall(self):
            return self.cursor.fetchall()
        def executemany(self, sql, values):
            for params in values:
                self.execute(sql, params)
        @property
        def rowcount(self):
            return self.cursor.rowcount

    with patch.object(transaction, 'cursor', return_value=FaultCursor()):
        store = CompetitorAuthorTagStore(lambda: transaction)
        with pytest.raises(pymysql.OperationalError):
            store.change_tags({'operation': 'add', 'accounts': [{'platform': 'wechat_channels', 'author_id': key}], 'tags': ['first', 'second']})
    assert len(inserts) == 2
    with connection.cursor() as c:
        c.execute('SELECT COUNT(*) AS n FROM competitor_author_tags WHERE author_id=%s', (key,))
        assert c.fetchone()['n'] == 0
