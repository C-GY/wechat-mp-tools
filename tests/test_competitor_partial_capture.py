"""Incomplete capture must still deliver the observations already saved."""
import json
from unittest.mock import MagicMock, patch

import pytest

from backend import channels, channels_refresh, competitor_monitor as monitor, mitm_proxy, pinchuang


AUTHOR = {"username": "author", "nickname": "作者"}
PAGING_ERROR = "第 7 页游标重复，重试 2 次后仍失败"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    channels_refresh.reset_refresh_task()
    monkeypatch.setattr(channels, "CHANNELS_FEEDS_FILE", tmp_path / "feeds.json")
    monkeypatch.setattr(channels, "CHANNELS_FAVORITES_FILE", tmp_path / "favorites.json")
    # An old, otherwise valid cached video must never enter this run.
    channels.CHANNELS_FEEDS_FILE.write_text(json.dumps({"author": [{
        "id": "stale", "capture_task_id": "old-task", "collected_at": 1,
        "oss_video_url": "https://fixture.invalid/stale.mp4",
    }]}), encoding="utf-8")
    task, _ = channels_refresh.start_refresh_task([AUTHOR], require_receipt=True)
    task_id = task["task_id"]
    channels_refresh.claim_refresh_command()
    hub = monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")
    hub.state["current_run"] = {
        "run_id": "run", "sync_batch_id": "batch", "status": "running",
        "creator_plan": [AUTHOR], "creators": [],
    }
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    manager = MagicMock()
    manager.start_selected_sync.return_value = {"batch_id": "transfer"}
    manager.snapshot.return_value = {"items": [
        {"batch_id": "transfer", "video_id": key, "status": "completed",
         "oss_url": f"https://fixture.invalid/{key}.mp4"} for key in ("v1", "v2")
    ]}
    with patch.object(channels_refresh, "start_refresh_task", return_value=(task, True)), \
         patch.object(monitor, "upload_manager", manager):
        yield hub, task_id, adapter, manager
    channels_refresh.reset_refresh_task()


def finish_capture(task_id, *, saved=("v1", "v2"), status="completed", error=PAGING_ERROR):
    if saved:
        mitm_proxy.save_synced_feeds("author", [{
            "id": key, "likeCount": 17,
            "objectDesc": {"mediaType": 4, "description": "作品", "media": [
                {"url": f"https://fixture.invalid/source-{key}.mp4"}
            ]},
        } for key in saved], task_id=task_id)
    return channels_refresh.update_refresh_task({
        "task_id": task_id, "status": status,
        "author_results": {"author": {
            "count": 999, "pagination_complete": False, "error": error,
            "pagination": {"page_number": 7, "attempt": 3, "reason": "repeated_cursor", "action": "fail"},
        }},
    })


def run_pipeline(hub, adapter):
    with patch.object(hub, "_validate_database_config"), \
         patch.object(pinchuang, "get_oss_config", return_value={"configured": True}), \
         patch.object(hub, "_database_adapter", return_value=adapter), \
         patch.object(hub, "_wait_for_wechat"), patch.object(hub, "_notifier") as notifier:
        hub._run_pipeline("run")
    return hub.state["current_run"], notifier.return_value


def test_incomplete_capture_delivers_saved_videos_and_keeps_partial_result(scenario):
    hub, task_id, adapter, manager = scenario
    finish_capture(task_id)
    run, notifier = run_pipeline(hub, adapter)
    assert manager.start_selected_sync.call_count == 1
    queued = manager.start_selected_sync.call_args.args[1]
    assert [v["id"] for v in queued] == ["v1", "v2"]
    assert all(v["video_url"].startswith("https://fixture.invalid/source-") for v in queued)
    rows = adapter.write_snapshots.call_args.args[0]
    assert [r["source_video_key"] for r in rows] == ["v1", "v2"]
    assert all(r["like_count"] == 17 and r["sync_batch_id"] == "batch" for r in rows)
    assert rows[0]["video_url"] == "https://fixture.invalid/v1.mp4"
    result = run["creators"][0]
    assert run["status"] == result["status"] == "partial"
    assert result["refreshed_videos"] == result["uploaded_videos"] == result["database_written"] == 2
    assert result["failed_items"] == 1
    assert PAGING_ERROR in result["message"]
    assert result["failures"][0]["stage"] == "capture"
    assert result["failures"][0]["capture_diagnostic"]["task_id"] == task_id
    assert "部分失败" in notifier.send.call_args.args[0]
    assert PAGING_ERROR in notifier.send.call_args.args[1]


@pytest.mark.parametrize("failed_ids", [("v2",), ("v1", "v2")])
def test_capture_and_transfer_errors_are_both_kept(scenario, failed_ids):
    hub, task_id, adapter, manager = scenario
    finish_capture(task_id)
    for item in manager.snapshot.return_value["items"]:
        if item["video_id"] in failed_ids:
            item.update(status="failed", stage="download", error="下载超时")
            item.pop("oss_url")
    run, notifier = run_pipeline(hub, adapter)
    result = run["creators"][0]
    assert result["status"] == ("partial" if len(failed_ids) == 1 else "failed")
    assert result["database_written"] == result["uploaded_videos"] == 2 - len(failed_ids)
    failures = hub.failure_page("run")["items"]
    assert len(failures) == 1 + len(failed_ids)
    assert {f["stage"] for f in failures} == {"capture", "download"}
    assert all("OSS 视频链接不能为空" not in f["error"] for f in failures)
    if len(failed_ids) == 2:
        assert "同步失败" in notifier.send.call_args.args[0]
    # Missing pages have unknown IDs; retry must refresh the entire author.
    with patch.object(hub, "start_run", return_value={}) as start:
        hub.retry_failed_run("run")
    assert start.call_args.kwargs["run_options"]["retry_selection"]["author"] == {"all": True}


@pytest.mark.parametrize("case", ["no_receipt", "missing_current_records", "cancelled", "wrong_task"])
def test_unconfirmed_stale_or_cancelled_capture_does_not_start_transfer(scenario, case):
    hub, task_id, adapter, manager = scenario
    finish_capture(task_id, saved=() if case == "no_receipt" else ("v1", "v2"),
                   status="cancelled" if case == "cancelled" else "completed")
    if case == "missing_current_records":
        channels.CHANNELS_FEEDS_FILE.write_text(json.dumps({"author": [{
            "id": "stale", "capture_task_id": "old-task", "collected_at": 9999999999,
        }]}), encoding="utf-8")
    if case == "wrong_task":
        def refresh(*args):
            hub._last_refresh_task_id = "different-task"
            raise channels_refresh.CaptureRefreshError(channels_refresh.get_refresh_status(task_id), "author")
        with patch.object(hub, "_refresh_author", side_effect=refresh):
            run, _ = run_pipeline(hub, adapter)
    else:
        run, _ = run_pipeline(hub, adapter)
    assert run["creators"][0]["status"] == "failed"
    manager.start_selected_sync.assert_not_called()
    adapter.write_snapshots.assert_not_called()


def test_partial_capture_checkpoint_survives_restart_without_losing_reason(scenario):
    hub, task_id, adapter, manager = scenario
    finish_capture(task_id)
    # Stop after observations and the capture error are durably saved.
    with patch.object(hub, "_sync_new_videos_to_oss", side_effect=pinchuang._RunStopping):
        with pytest.raises(pinchuang._RunStopping):
            hub._run_creator("run", "batch", AUTHOR, adapter)
    saved = hub.journal.checkpoint("run", "author")
    assert len(saved["observations"]) == 2
    assert saved["capture_failures"][0]["capture_diagnostic"]["pagination"]["page_number"] == 7
    hub._persist_state_locked()
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    with patch.object(restarted, "_refresh_author") as refresh:
        run, _ = run_pipeline(restarted, adapter)
    refresh.assert_not_called()
    assert run["creators"][0]["status"] == run["status"] == "partial"
    assert run["database_written"] == 2
    assert PAGING_ERROR in run["creators"][0]["message"]
    assert restarted.failure_page("run")["items"][0]["capture_diagnostic"]["task_id"] == task_id


def test_stalled_capture_delivers_confirmed_records_and_keeps_timeout(scenario):
    hub, task_id, adapter, manager = scenario
    mitm_proxy.save_synced_feeds('author', [
        {'id': key, 'objectDesc': {'mediaType': 4, 'media': []}} for key in ('v1', 'v2')
    ], task_id=task_id)
    seconds = [0]
    def sleep(_):
        seconds[0] += 30
    with patch.object(pinchuang.time, 'monotonic', side_effect=lambda: seconds[0]), \
         patch.object(pinchuang.time, 'sleep', side_effect=sleep):
        run, _ = run_pipeline(hub, adapter)
    result = run['creators'][0]
    assert result['status'] == 'partial'
    assert result['refreshed_videos'] == result['database_written'] == 2
    assert [v['id'] for v in manager.start_selected_sync.call_args.args[1]] == ['v1', 'v2']
    assert result['failed_items'] == 1
    failure = hub.failure_page('run')['items'][0]
    assert failure['stage'] == 'capture'
    assert failure['capture_diagnostic']['timeout']['reason'] == 'no_saved_progress'
    assert failure['capture_diagnostic']['timeout']['elapsed_seconds'] == 300
    assert channels_refresh.get_refresh_status()['status'] == 'failed'
