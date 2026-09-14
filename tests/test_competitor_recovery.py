import copy
import json
import time
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest

from backend import competitor_monitor as monitor, pinchuang
from backend.monitor_journal import MonitorJournal


AUTHOR = {"username": "author-1", "nickname": "作者"}


def run_state(status="running"):
    return {"run_id": "run-1", "sync_batch_id": "batch-1", "status": status, "phase": "uploading_oss",
            "started_at": "2026-09-14 10:00:00.000", "finished_at": "", "creator_plan": [AUTHOR], "creators": [],
            "transfer": {"download_workers": 2, "upload_workers": 2}}


@pytest.fixture
def hub(tmp_path):
    return monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")


def test_all_failures_survive_restart_and_are_paged(hub):
    hub.state["current_run"] = run_state()
    failures = [{"video_id": str(i), "error": "download interrupted", "stage": "download"} for i in range(103)]
    hub._creator_result("run-1", {"author_id": "author-1", "status": "partial", "failed_items": 103, "failures": failures})
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    assert len(restarted.state["current_run"]["creators"][0]["failures"]) == 3
    pages = [restarted.failure_page("run-1", offset=offset) for offset in [0, 50, 100]]
    assert [len(p["items"]) for p in pages] == [50, 50, 3]
    assert {f["video_id"] for p in pages for f in p["items"]} == {str(i) for i in range(103)}
    assert pages[-1]["legacy_details_missing"] == 0


def test_legacy_truncation_is_explicit_and_retries_missing_database_rows(hub):
    run = run_state("interrupted")
    run["creators"] = [{"author_id": "author-1", "author_name": "作者", "status": "partial", "failed_items": 421,
                        "failures": [{"video_id": str(i), "error": "legacy"} for i in range(100)]}]
    hub.state["current_run"] = run
    hub._persist_state_locked()
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    page = restarted.failure_page("run-1")
    assert page["total"] == 100 and page["legacy_details_missing"] == 321
    with patch.object(restarted, "start_run", return_value={}) as start:
        restarted.retry_failed_run("run-1")
    options = start.call_args.kwargs["run_options"]
    assert options["retry_selection"]["author-1"] == {"missing_only": True}
    assert options["sync_batch_id"] == "batch-1"


def test_restart_uses_observation_and_receipt_without_refreshing_success(hub):
    state = run_state()
    state["creator_plan"] = [{"username": "finished", "nickname": "已完成"}, AUTHOR]
    hub.state["current_run"] = state
    hub._persist_state_locked()
    # Durable result exists but the JSON update was lost in a crash.
    hub.journal.save_result("run-1", {"author_id": "finished", "status": "completed", "database_written": 1, "failed_items": 0, "failures": []})
    observation = {"id": "video-1", "collected_at": 1789344000, "rpa_payload": {"likeCount": 17}}
    hub.journal.save_checkpoint("run-1", "author-1", {"observations": [observation],
        "started_at": "2026-09-14 10:01:00.000", "uploads": {"video-1": "https://fixture.invalid/receipt.mp4"}})
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    restarted.config["schedule"]["creator_interval_seconds"] = 0
    assert restarted.recoverable_run_id == "run-1"
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    with patch.object(restarted, "_validate_database_config"), patch.object(pinchuang, "get_oss_config", return_value={"configured": True}), patch.object(restarted, "_database_adapter", return_value=adapter), patch.object(restarted, "_wait_for_wechat"), patch.object(restarted, "_refresh_author") as refresh, patch.object(restarted, "_load_author_videos", return_value=[]), patch.object(restarted, "_sync_new_videos_to_oss", return_value=(1, [])) as transfer:
        restarted._run_pipeline("run-1")
    refresh.assert_not_called()
    assert transfer.call_args.args[2][0]["oss_video_url"].endswith("receipt.mp4")
    rows = adapter.write_snapshots.call_args.args[0]
    assert rows[0]["like_count"] == 17 and rows[0]["sync_batch_id"] == "batch-1"
    assert restarted.state["current_run"]["status"] == "completed"
    assert restarted.state["current_run"]["database_written"] == 2


def test_paused_restart_does_not_automatically_resume(hub):
    hub.state["current_run"] = run_state("paused")
    hub._persist_state_locked()
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    assert restarted.recoverable_run_id is None
    assert restarted.state["current_run"]["status"] == "paused"
    with patch.object(pinchuang.threading.Thread, "start"):
        restored = restarted.resume_run()
    assert restored["run_id"] == "run-1" and restored["sync_batch_id"] == "batch-1"
    assert restored["status"] == "queued"


@pytest.mark.parametrize("selection", [{"ids": ["bad"]}, {"all": True, "preserve_observations": True}])
def test_retry_only_selected_failures_preserves_original_metrics_with_fresh_url(hub, selection):
    observation = {"id": "bad", "collected_at": 1789344000, "rpa_payload": {"likeCount": 5}, "video_url": "https://source.invalid/old"}
    hub.journal.save_checkpoint("old-run", "author-1", {"observations": [observation]})
    hub.state["current_run"] = {**run_state(), "retry_source_run_id": "old-run",
                                "retry_selection": {"author-1": selection}}
    fresh = [{"id": key, "collected_at": time.time() + 10, "rpa_payload": {"likeCount": 999}, "video_url": "https://source.invalid/fresh"} for key in ["bad", "already-done"]]
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    def upload(*args):
        assert [v["id"] for v in args[2]] == ["bad"]
        assert args[2][0]["video_url"].endswith("fresh")
        hub._uploaded_video_urls = {"bad": "https://oss.invalid/retried.mp4"}
        return 1, []
    with patch.object(hub, "_refresh_author", return_value=2), patch.object(hub, "_load_author_videos", return_value=fresh), patch.object(hub, "_sync_new_videos_to_oss", side_effect=upload):
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert result["status"] == "completed"
    assert adapter.write_snapshots.call_args.args[0][0]["like_count"] == 5


def test_database_failure_compensation_keeps_original_observation_scope(hub):
    hub.state["current_run"] = run_state("failed")
    hub._creator_result("run-1", {"author_id": "author-1", "status": "failed", "failed_items": 1,
        "failures": [{"error": "commit response lost", "stage": "database_write"}]})
    with patch.object(hub, "start_run", return_value={}) as start:
        hub.retry_failed_run("run-1")
    selection = start.call_args.kwargs["run_options"]["retry_selection"]["author-1"]
    assert selection == {"all": True, "preserve_observations": True}


def test_database_failure_preserves_observed_progress_and_true_start(hub):
    hub._current_creator_progress = {"started_at": "2026-09-14 10:01:00", "refreshed_videos": 100,
        "new_videos": 100, "uploaded_videos": 95, "stage": "database_write"}
    result = hub._creator_failure_result(AUTHOR, RuntimeError("connection lost"), "wrong")
    assert result["started_at"] == "2026-09-14 10:01:00"
    assert result["refreshed_videos"] == 100 and result["uploaded_videos"] == 95
    assert result["database_outcome"] == "unknown"


def test_failure_api_and_conflict_when_retrying_running_task(hub):
    hub.state["current_run"] = run_state()
    hub._creator_result("run-1", {"author_id": "author-1", "status": "partial", "failed_items": 1,
        "failures": [{"video_id": "bad", "error": "interrupted", "stage": "download"}]})
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    with patch.object(monitor, "competitor_monitor_hub", hub), patch.object(hub, "_validate_database_config"), patch.object(pinchuang, "get_oss_config", return_value={"configured": True}):
        client = app.test_client()
        response = client.get('/api/competitor_monitor/runs/run-1/failures')
        assert response.json["items"][0]["video_id"] == "bad"
        assert client.get('/api/competitor_monitor/runs/run-1/failures?limit=999').status_code == 400
        hub.worker = MagicMock()
        hub.worker.is_alive.return_value = True
        assert client.post('/api/competitor_monitor/runs/run-1/retry-failed').status_code == 409
        assert hub.state["current_run"]["run_id"] == "run-1"
