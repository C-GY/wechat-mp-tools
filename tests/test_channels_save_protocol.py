import http.client
import json
import threading
import time
from unittest.mock import Mock

import pytest

from test_proxy_lifecycle import manager
from backend import channels, channels_refresh, channels_save_jobs, mitm_proxy
from backend.channels_storage import feed_store, FeedStore


@pytest.fixture
def capture_store(tmp_path, monkeypatch):
    monkeypatch.setattr(channels,'CHANNELS_FEEDS_FILE',tmp_path/'feeds.json')
    monkeypatch.setattr(channels,'CHANNELS_FAVORITES_FILE',tmp_path/'favorites.json')
    channels_refresh.reset_refresh_task()
    store = feed_store(channels.CHANNELS_FEEDS_FILE)
    store.ensure_ready()
    task,_ = channels_refresh.start_refresh_task([{'username':'author'}],require_receipt=True)
    command = channels_refresh.claim_refresh_command('page-owner')
    yield store, task['task_id'], command['lease_token']
    channels_refresh.reset_refresh_task()


def payload(task,lease,page='1',video='a'):
    return {'task_id':task,'lease_token':lease,'page_id':page,'username':'author',
            'feeds':[{'id':video,'contact':{'nickname':'Fixture'},'objectDesc':{'mediaType':4,'media':[]}}]}


def wait_saved(body):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        status=channels_save_jobs.page_status(body)
        if status['status'] in {'saved','failed'}:
            return status
        time.sleep(.02)
    raise AssertionError('page write did not finish')


def test_slow_save_keeps_real_proxy_healthy_and_receipt_survives_rebuild(manager,capture_store,monkeypatch):
    proxy,switch=manager
    store,task,lease=capture_store
    entered,release=threading.Event(),threading.Event()
    original=store._merge
    def slow(db,author,items):
        entered.set()
        assert release.wait(3)
        return original(db,author,items)
    monkeypatch.setattr(store,'_merge',slow)
    proxy.watchdog_interval=.03
    proxy.watchdog_probe_timeout=.1
    proxy.start()
    old_thread=proxy.thread
    switch.reset_mock()
    body=payload(task,lease)
    conn=http.client.HTTPConnection('127.0.0.1',proxy.port,timeout=.8)
    try:
        conn.request('POST','http://channels.weixin.qq.com/__wx_channels_api/sync-feed',json.dumps(body),{'Content-Type':'application/json'})
        response=conn.getresponse()
        assert json.loads(response.read())['data']['status']=='pending'
        assert entered.wait(1)
        for _ in range(5):
            assert proxy.is_healthy(timeout=.2)
            time.sleep(.04)
        assert proxy.thread is old_thread
        assert not any(c.args[0] is False for c in switch.call_args_list)
    finally:
        release.set()
        conn.close()
    assert wait_saved(body)['saved_ids']==['a']
    proxy.stop(); proxy.start()
    # A lost response or listener restart is resolved from the committed receipt.
    assert channels_save_jobs.page_status(body)['saved_ids']==['a']
    assert channels_save_jobs.submit_page(body)['status']=='saved'
    second=payload(task,lease,'2','b')
    channels_save_jobs.submit_page(second)
    assert wait_saved(second)['saved_ids']==['b']
    channels_save_jobs.save_checkpoint({**second,'page_number':2,'marker':'','complete':True})
    persisted=FeedStore(store.source,store.path).resume(task,'author')
    assert persisted['complete'] and set(persisted['saved_ids'])=={'a','b'}
    assert channels_refresh.get_refresh_status()['persisted_counts']=={'author':2}


def test_old_page_cannot_commit_after_ownership_changes(capture_store,monkeypatch):
    store,task,lease=capture_store
    entered,release=threading.Event(),threading.Event()
    original=mitm_proxy.save_synced_feeds
    def blocked(*args,**kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args,**kwargs)
    monkeypatch.setattr(mitm_proxy,'save_synced_feeds',blocked)
    body=payload(task,lease)
    channels_save_jobs.submit_page(body)
    assert entered.wait(1)
    try:
        previous=channels_refresh._refresh_task['updated_at']
        channels_refresh._refresh_task['updated_at']=previous-100
        next_command=channels_refresh.claim_refresh_command('new-owner')
        assert next_command['lease_token']!=lease
    finally:
        release.set()
    with pytest.raises(ValueError,match='接替'):
        channels_save_jobs.page_status(body)
    future=channels_save_jobs._jobs[(task,'author','1')]['future']
    with pytest.raises(ValueError,match='接替'):
        future.result(3)
    assert store.author('author') is None


def test_receipt_failure_does_not_advance_checkpoint(capture_store,monkeypatch):
    store,task,lease=capture_store
    def fail(*args,**kwargs):
        raise OSError('disk full fixture')
    monkeypatch.setattr(store,'_merge',fail)
    body=payload(task,lease)
    channels_save_jobs.submit_page(body)
    assert wait_saved(body)['status']=='failed'
    assert channels_refresh.get_refresh_status()['persisted_counts'].get('author',0)==0
    with pytest.raises(ValueError,match='尚未确认'):
        channels_save_jobs.save_checkpoint({**body,'page_number':2,'marker':'next'})


def test_stall_notifications_are_grouped_without_losing_creator_records(tmp_path):
    from backend.competitor_monitor import CompetitorMonitorHub
    hub=CompetitorMonitorHub(tmp_path/'config.json',tmp_path/'state.json')
    hub.state['current_run']={'run_id':'run','sync_batch_id':'batch','creators':[]}
    notifier=Mock()
    notifier.send.return_value={'sent':True}
    failure={'capture_diagnostic':{'timeout':{'reason':'no_saved_progress'}},'stage':'capture'}
    for index in range(3):
        result={'author_id':str(index),'status':'partial','failed_items':1,'message':'stalled','failures':[failure]}
        hub._creator_result('run',result)
        hub._notify_creator_failure(notifier,hub.state['current_run'],str(index),result)
    hub._notify_stall_summary(notifier,'run','batch')
    hub._notify_stall_summary(notifier,'run','batch')
    assert notifier.send.call_count==2
    assert len(hub.state['current_run']['creators'])==3
    assert len(hub.journal.failures('run')['items'])==3


def test_unsent_stall_summary_is_not_marked_as_sent(tmp_path):
    from backend.competitor_monitor import CompetitorMonitorHub
    hub=CompetitorMonitorHub(tmp_path/'config.json',tmp_path/'state.json')
    hub.state['current_run']={'run_id':'run','capture_stall_notices':['a','b']}
    notifier=Mock()
    notifier.send.return_value={'sent':False,'error':'fixture unavailable'}
    hub._notify_stall_summary(notifier,'run','batch')
    assert not hub.state['current_run'].get('capture_stall_summary_sent')
    notifier.send.return_value={'sent':True}
    hub._notify_stall_summary(notifier,'run','batch')
    hub._notify_stall_summary(notifier,'run','batch')
    assert notifier.send.call_count==2
    assert hub.state['current_run']['capture_stall_summary_sent']
