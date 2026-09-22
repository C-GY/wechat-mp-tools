import asyncio
from backend.channels_storage import read_feeds, feed_store, FeedStore
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend import channels, channels_refresh, mitm_proxy


@pytest.fixture
def capture(tmp_path, monkeypatch):
    channels_refresh.reset_refresh_task()
    monkeypatch.setattr(channels, "CHANNELS_FEEDS_FILE", tmp_path / "feeds.json")
    monkeypatch.setattr(channels, "CHANNELS_FAVORITES_FILE", tmp_path / "favorites.json")
    task, _ = channels_refresh.start_refresh_task([{"username": "author"}], require_receipt=True)
    channels_refresh.claim_refresh_command()
    yield task["task_id"]
    channels_refresh.reset_refresh_task()


def feed(video_id="video-1"):
    return {"id": video_id, "contact": {"nickname": "作者"}, "objectDesc": {"mediaType": 4, "media": []}}


def finish(task_id, count=1, complete=True):
    return channels_refresh.update_refresh_task({"task_id": task_id, "status": "completed",
        "total_videos": count, "completed_authors": 1,
        "author_results": {"author": {"count": count, "pagination_complete": complete}}})


def test_capture_ack_is_persisted_and_correlated_to_task(capture):
    receipt = mitm_proxy.save_synced_feeds("author", [feed()], task_id=capture)
    assert receipt == {"saved_ids": ["video-1"], "capture_task_id": capture}
    saved = read_feeds(channels.CHANNELS_FEEDS_FILE)
    assert saved["author"][0]["capture_task_id"] == capture
    assert saved["author"][0]["rpa_payload"] == feed()
    assert finish(capture)["status"] == "completed"


def test_actual_sync_endpoint_returns_receipt_and_reports_storage_failure(capture):
    http = pytest.importorskip("mitmproxy.http")
    payload = json.dumps({"username": "author", "feeds": [feed()], "task_id": capture}).encode()
    def send():
        flow = SimpleNamespace(request=http.Request.make("POST", "https://channels.weixin.qq.com/__wx_channels_api/sync-feed", payload, {"Content-Type": "application/json"}), response=None)
        asyncio.run(mitm_proxy.ChannelsAddon().request(flow))
        return flow.response
    response = send()
    assert response.status_code == 200
    assert json.loads(response.content)["data"]["saved_ids"] == ["video-1"]
    with patch.object(FeedStore, "merge", side_effect=OSError("disk full")):
        response = send()
    assert response.status_code == 500
    assert json.loads(response.content)["code"] != 0
    assert json.loads(response.content)["msg"] == "disk full"


@pytest.mark.parametrize("count,complete", [(2, True), (1, False)])
def test_partial_or_unconfirmed_capture_cannot_finish(capture, count, complete):
    mitm_proxy.save_synced_feeds("author", [feed()], task_id=capture)
    assert finish(capture, count, complete)["status"] == "failed"


def test_save_failure_cannot_be_acknowledged(capture):
    with patch.object(FeedStore, "merge", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            mitm_proxy.save_synced_feeds("author", [feed()], task_id=capture)
    assert finish(capture)["status"] == "failed"
    assert not channels.CHANNELS_FEEDS_FILE.exists()


def test_wrong_task_and_corrupt_cache_never_overwrite_good_evidence(capture):
    with pytest.raises(ValueError, match="任务"):
        mitm_proxy.save_synced_feeds("author", [feed()], task_id="old-task")
    channels.CHANNELS_FEEDS_FILE.write_text('{"broken":', encoding="utf-8")
    with pytest.raises(ValueError):
        mitm_proxy.save_synced_feeds("author", [feed()], task_id=capture)
    assert channels.CHANNELS_FEEDS_FILE.read_text(encoding="utf-8") == '{"broken":'


def test_legacy_completion_requires_reopening_capture_page(capture):
    result = channels_refresh.update_refresh_task({"task_id": capture, "status": "completed", "total_videos": 9})
    assert result["status"] == "failed"
    assert "重新打开" in result["message"]


def test_timed_out_refresh_releases_command_for_next_author(tmp_path):
    from backend import competitor_monitor, pinchuang
    channels_refresh.reset_refresh_task()
    hub = competitor_monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")
    try:
        with patch.object(pinchuang.time, "monotonic", side_effect=[0, 1801]):
            with pytest.raises(channels_refresh.CaptureRefreshError, match="没有新增已保存作品"):
                hub._refresh_author("run-1", {"username": "author"})
        assert channels_refresh.get_refresh_status()["status"] == "failed"
        _, created = channels_refresh.start_refresh_task([{"username": "next"}])
        assert created
    finally:
        channels_refresh.reset_refresh_task()


def test_capture_and_upload_receipt_do_not_overwrite_each_other(capture):
    from backend import oss
    mitm_proxy.save_synced_feeds("author", [feed()], task_id=capture)
    read, release, capture_started = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_write = FeedStore.patch_existing
    def delayed_write(store, *args):
        read.set()
        assert release.wait(3)
        return original_write(store, *args)
    def upload_receipt():
        try:
            oss.OSSUploadManager._save_video_result({"username": "author", "video_id": "video-1"},
                {"url": "https://fixture.invalid/1.mp4", "object_key": "1.mp4"})
        except Exception as exc:
            errors.append(exc)
    def capture_page():
        capture_started.set()
        try:
            mitm_proxy.save_synced_feeds("author", [feed("video-2")], task_id=capture)
        except Exception as exc:
            errors.append(exc)
    with patch.object(FeedStore, "patch_existing", delayed_write):
        first = threading.Thread(target=upload_receipt)
        second = threading.Thread(target=capture_page)
        first.start()
        try:
            assert read.wait(3)
            second.start()
            assert capture_started.wait(3)
        finally:
            release.set()
            first.join(3)
            if second.ident:
                second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    rows = {v["id"]: v for v in read_feeds(channels.CHANNELS_FEEDS_FILE)["author"]}
    assert set(rows) == {"video-1", "video-2"}
    assert rows["video-1"]["oss_video_url"].endswith("1.mp4")


def test_real_injected_pagination_handles_short_pages_and_storage_errors():
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('injection_scripts/src/automation.js', 'utf8');
const fn = source.slice(source.indexOf('  async function refreshFavoriteAuthor'), source.indexOf('  async function runRemoteFavoritesRefresh'));
let cancelled = false, circuitOpen = false, my_username = '', PAGE_JITTER_MS = 0;
const esc = x => x, setPanel = () => {}, noteFailure = () => {};
const reportRemoteProgress = async () => {}, jitterSleep = async () => {};
const logCall = () => {};
const callWithRetry = async (_name, call) => call();
let pages, saved, calls, failSave;
const object = id => ({id, objectDesc:{mediaType:4}});
const WXU = {API:{finderUserPage:async () => {calls++; return pages.length>1 ? pages.shift() : pages[0];}}, request:async ({body}) => {
  if (failSave) return [new Error('disk full'), null];
  saved.push(...body.feeds.map(v=>v.id));
  return [null, {saved_ids:body.feeds.map(v=>v.id), capture_task_id:body.task_id}];
}};
eval(fn);
(async () => {
  calls=0; saved=[];
  pages=[{errCode:0,data:{object:[object('a')],lastBuffer:'next'}},{errCode:0,data:{object:[object('b')],lastBuffer:''}}];
  assert.equal(await refreshFavoriteAuthor({username:'author'},'task',1,1,{completed:0,failed:0,videos:0}),2);
  assert.equal(calls,2); assert.deepEqual(saved,['a','b']);
  calls=0; failSave=true;
  pages=[{errCode:0,data:{object:[object('a')],lastBuffer:''}}];
  await assert.rejects(()=>refreshFavoriteAuthor({username:'author'},'task',1,1,{completed:0,failed:0,videos:0}),/disk full/);
  failSave=false; pages=[{errCode:0,data:{object:[object('a')],lastBuffer:'same'}},{errCode:0,data:{object:[object('b')],lastBuffer:'same'}}];
  await assert.rejects(()=>refreshFavoriteAuthor({username:'author'},'task',1,1,{completed:0,failed:0,videos:0}),/游标重复/);
  console.log('short-page, receipt-error, repeated-cursor: passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pagination_failure_details_survive_capture_and_monitor_journal(capture, tmp_path):
    from backend import competitor_monitor

    mitm_proxy.save_synced_feeds('author', [feed()], task_id=capture)
    diagnostic = {
        'page_number': 7, 'attempt': 3, 'raw_count': 0, 'video_count': 0,
        'saved_count': 1, 'input_cursor_present': True, 'output_cursor_present': True,
        'cursor_changed': False, 'cursor_repeated': True, 'has_more': True,
        'continue_flag': None, 'reason': 'repeated_cursor', 'action': 'fail',
        'lastBuffer': 'SECRET_CURSOR_MUST_NOT_BE_SAVED',
    }
    status = channels_refresh.update_refresh_task({
        'task_id': capture, 'status': 'completed',
        'author_results': {'author': {'count': 999, 'pagination_complete': False,
                                     'error': '第 7 页游标重复，重试 2 次后仍失败', 'pagination': diagnostic}},
    })
    assert status['status'] == 'failed'
    assert status['total_videos'] == 1
    assert status['author_results']['author']['pagination']['page_number'] == 7
    assert 'SECRET_CURSOR' not in json.dumps(status)

    hub = competitor_monitor.CompetitorMonitorHub(tmp_path / 'config.json', tmp_path / 'state.json')
    hub.state['current_run'] = {'run_id': 'run', 'sync_batch_id': 'batch', 'status': 'running', 'creators': []}
    hub._current_creator_progress = {'stage': 'capture'}
    with patch.object(channels_refresh, 'start_refresh_task', return_value=({'task_id': capture}, True)):
        with pytest.raises(RuntimeError) as caught:
            hub._refresh_author('run', {'username': 'author'})
    result = hub._creator_failure_result({'username': 'author'}, caught.value, '2026-09-14 00:00:00')
    hub._creator_result('run', result)
    assert result['status'] == 'failed'
    assert result['refreshed_videos'] == 1  # confirmed storage count, not the page's 999
    restarted = competitor_monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    failure = restarted.journal.failures('run')['items'][0]
    assert failure['capture_diagnostic']['task_id'] == capture
    assert failure['capture_diagnostic']['pagination']['page_number'] == 7
    assert failure['capture_diagnostic']['pagination']['attempt'] == 3
    assert 'SECRET_CURSOR' not in json.dumps(failure)
