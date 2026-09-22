import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.channels_storage import feed_store, read_feeds


def test_migration_preserves_all_fields_duplicates_and_source(tmp_path):
    source = tmp_path / 'feeds.json'
    original = {'昵称': [{'id': 12345678901234567890, 'rpa_payload': {'x': '中文'}, 'oss_video_url': 'saved'},
                       {'id': 12345678901234567890, 'description': 'duplicate'}, {'description': 'no id'}], 'empty': []}
    source.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8-sig')
    before = source.read_bytes()
    store = feed_store(source)
    assert dict(read_feeds(source)) == original
    assert source.read_bytes() == before
    assert store.status()['issues'] == 2
    export = tmp_path / 'export.json'
    store.export_json(export)
    assert json.loads(export.read_text(encoding='utf-8')) == original


def test_failed_migration_does_not_publish_partial_data_and_can_retry(tmp_path):
    source = tmp_path / 'feeds.json'
    source.write_text('{"first":[{"id":"1"}],"broken":[', encoding='utf-8')
    store = feed_store(source)
    with pytest.raises(ValueError):
        store.ensure_ready()
    assert store.status()['status'] == 'failed'
    source.write_text('{"fixed":[{"id":"2"}]}', encoding='utf-8')
    store.ensure_ready()
    assert dict(read_feeds(source)) == {'fixed': [{'id':'2'}]}


def test_page_and_upload_updates_merge_without_whole_json_rewrite(tmp_path):
    source = tmp_path / 'feeds.json'
    source.write_text(json.dumps({'other': [{'id':'old','payload':'x'*100000}], 'author':[{'id':'1','likes':1}]}))
    before = source.read_bytes()
    store = feed_store(source)
    store.ensure_ready()
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(store.merge, 'author', [{'id':'1','likes':2}, {'id':'2'}])
        b = pool.submit(store.merge, 'author', [{'id':'1','oss_video_url':'verified'}])
        a.result(); b.result()
    rows = {str(v['id']):v for v in read_feeds(source)['author']}
    assert rows['1'] == {'id':'1','likes':2,'oss_video_url':'verified'}
    assert '2' in rows
    assert source.read_bytes() == before
    backup = tmp_path / 'backup.sqlite3'
    store.backup(backup)
    assert backup.exists()


def test_page_receipt_is_atomic_idempotent_and_rejects_conflicting_replay(tmp_path):
    store = feed_store(tmp_path / 'feeds.json')
    page = {'task_id':'task','page_id':'1:1','digest':'abc'}
    first = store.merge('author',[{'id':'a','capture_task_id':'task'}], page=page)
    assert first['saved_ids'] == ['a']
    assert store.merge('author',[{'id':'a','description':'must not overwrite'}], page=page) == first
    assert 'description' not in store.author('author')[0]
    with pytest.raises(ValueError, match='不一致'):
        store.merge('author',[{'id':'b'}], page={**page,'digest':'different'})
    assert store.receipt('task','author','1:1') == first
    store.checkpoint('task','author','1:1',{'page_number':2,'marker':'opaque','complete':False})
    assert store.resume('task','author')['page_number'] == 2
    assert store.resume('task','author')['saved_ids'] == ['a']


def test_alias_merge_retains_duplicate_legacy_rows(tmp_path):
    source = tmp_path / 'feeds.json'
    original = [{'id':'1','description':'first'},{'id':'1','description':'duplicate'}]
    source.write_text(json.dumps({'nickname':original}))
    store = feed_store(source)
    store.merge('username',[{'id':'2'}],alias='nickname')
    assert store.author('nickname') == original
    assert {row['id'] for row in store.author('username')} == {'1','2'}


def test_low_disk_space_leaves_source_untouched_and_allows_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend import channels_storage
    source = tmp_path / 'feeds.json'
    source.write_text('{"a":[{"id":"1"}]}')
    before = source.read_bytes()
    store = feed_store(source)
    with monkeypatch.context() as limited:
        limited.setattr(channels_storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
        with pytest.raises(OSError,match='空间不足'):
            store.ensure_ready()
    assert source.read_bytes() == before
    store.ensure_ready()
    assert store.author('a') == [{'id':'1'}]


def test_process_killed_mid_migration_rolls_back_and_restarts(tmp_path):
    import subprocess
    import sys
    from backend.channels_storage import FeedStore
    source, database = tmp_path / 'feeds.json', tmp_path / 'feeds.sqlite3'
    original = {'a':[{'id':str(n)} for n in range(1000)]}
    source.write_text(json.dumps(original))
    program = '''
import os, sys
from backend.channels_storage import FeedStore, _JsonStream
original = _JsonStream.value
count = 0
def crash(self):
    global count
    count += 1
    if count == 400:
        os._exit(73)
    return original(self)
_JsonStream.value = crash
FeedStore(sys.argv[1],sys.argv[2]).ensure_ready()
'''
    result = subprocess.run([sys.executable,'-c',program,str(source),str(database)], timeout=30)
    assert result.returncode == 73
    store = FeedStore(source,database)
    store.ensure_ready()
    assert store.author('a') == original['a']
    with store.connection() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
