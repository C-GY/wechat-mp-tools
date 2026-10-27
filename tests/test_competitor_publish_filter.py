"""Publication cutoff configuration and the real monitor pipeline, with no remote IO."""
import copy
from datetime import datetime, timezone
import time
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from backend import competitor_monitor as monitor, pinchuang


AUTHOR = {"username": "author-1", "nickname": "测试作者"}
NOW = datetime(2026, 10, 19, 10, tzinfo=pinchuang.BEIJING_TZ)
SCOPE = {"mode": "custom", "before_date": "2026-09-01"}


@pytest.fixture
def hub(tmp_path):
    return monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json", now=lambda: NOW)


@pytest.mark.parametrize("mode,expected", [
    ("year", "2025-10-19"), ("half_year", "2026-04-19"),
    ("three_months", "2026-07-19"), ("month", "2026-09-19"), ("week", "2026-10-12"),
])
def test_presets_mean_before_the_date_rather_than_recent_period(mode, expected):
    assert monitor.resolve_publish_filter({"mode": mode}, NOW) == {"mode": mode, "before_date": expected}


@pytest.mark.parametrize("now,mode,expected", [
    (datetime(2026, 3, 31), "month", "2026-02-28"),
    (datetime(2024, 2, 29), "year", "2023-02-28"),
    (datetime(2024, 5, 31), "three_months", "2024-02-29"),
    (datetime(2026, 10, 31, 17, tzinfo=timezone.utc), "month", "2026-10-01"),
    (datetime(2026, 1, 3), "week", "2025-12-27"),
])
def test_calendar_months_leap_years_and_beijing_day_boundary(now, mode, expected):
    assert monitor.resolve_publish_filter({"mode": mode}, now)["before_date"] == expected


@pytest.mark.parametrize("value", [None, [], "week", {"mode": []}, {"mode": "recent"},
    {"mode": "custom", "before_date": ""}, {"mode": "custom", "before_date": "2026-02-30"},
    {"mode": "custom", "before_date": "2026-9-1"}, {"mode": "custom", "before_date": True},
    {"mode": "custom", "before_date": "0999-12-31"}])
def test_invalid_filter_is_rejected_without_overwriting_saved_config(hub, value):
    hub.save_config({"publish_filter": SCOPE})
    before = hub.config_path.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    with patch.object(monitor, "competitor_monitor_hub", hub):
        response = app.test_client().post("/api/competitor_monitor/config", json={"publish_filter": value})
    assert response.status_code == 400
    assert hub.config_path.read_bytes() == before


def test_persistence_backup_and_both_legacy_schemas(hub):
    assert hub.config["publish_filter"] == monitor.DEFAULT_PUBLISH_FILTER
    assert hub.save_config({"publish_filter": SCOPE})["publish_filter"] == SCOPE
    hub.save_config({"schedule": {"creator_interval_seconds": 15}})
    backup = hub.export_config_backup()
    assert backup["config"]["publish_filter"] == SCOPE
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    assert restarted.config["publish_filter"] == SCOPE
    restarted.save_config({"publish_filter": {"mode": "week"}})
    restarted.import_config_backup(backup)
    assert restarted.get_config()["publish_filter"] == SCOPE
    for include_transfer in (True, False):
        legacy = copy.deepcopy(backup)
        del legacy["config"]["publish_filter"]
        if not include_transfer:
            del legacy["config"]["transfer"]
        restarted.import_config_backup(legacy)
        assert restarted.config["publish_filter"] == monitor.DEFAULT_PUBLISH_FILTER
    assert "publish_filter" not in pinchuang.DEFAULT_CONFIG


@pytest.mark.parametrize("trigger", ["manual", "scheduled"])
def test_start_freezes_date_and_resume_retry_do_not_use_new_settings(hub, trigger):
    hub.save_config({"publish_filter": {"mode": "month"}})
    with patch.object(hub, "_validate_database_config"), \
         patch.object(pinchuang, "get_oss_config", return_value={"configured": True}), \
         patch.object(pinchuang.threading.Thread, "start"):
        original = hub.start_run(trigger=trigger)
    assert original["publish_filter"] == {"mode": "month", "before_date": "2026-09-19"}
    hub.save_config({"publish_filter": {"mode": "year"}})
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    with patch.object(pinchuang.threading.Thread, "start"):
        resumed = restarted.resume_saved_run(original["run_id"])
    assert resumed["publish_filter"] == original["publish_filter"]
    restarted.state["current_run"]["status"] = "partial"
    restarted.state["current_run"]["creator_plan"] = [AUTHOR]
    restarted._creator_result(original["run_id"], {"author_id": AUTHOR["username"], "failed_items": 1,
        "failures": [{"video_id": "old", "stage": "download", "error": "fixture"}]})
    with patch.object(restarted, "_validate_database_config"), \
         patch.object(pinchuang, "get_oss_config", return_value={"configured": True}), \
         patch.object(pinchuang.threading.Thread, "start"):
        retried = restarted.retry_failed_run(original["run_id"])
    assert retried["publish_filter"] == original["publish_filter"]


def observation(key, stamp=None, **extra):
    return {"id": key, "collected_at": time.time() + 100,
            "rpa_payload": {"createtime": stamp, "likeCount": 7}, **extra}


def execute_creator(hub, videos, *, resume=False):
    hub.state["current_run"] = {"run_id": "run", "publish_filter": copy.deepcopy(SCOPE)}
    hub.config["publish_filter"] = {"mode": "all", "before_date": ""}  # Must use the run's frozen scope.
    if resume:
        hub.journal.save_checkpoint("run", AUTHOR["username"], {"observations": videos})
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    selected = []

    def upload(_, author, pending):
        selected.extend(v["id"] for v in pending)
        hub._uploaded_video_urls = {v["id"]: f'https://fixture.invalid/{v["id"]}.mp4' for v in pending}
        return len(pending), []

    with patch.object(hub, "_refresh_author", return_value=len(videos)) as refresh, \
         patch.object(hub, "_load_author_videos", return_value=videos), \
         patch.object(hub, "_sync_new_videos_to_oss", side_effect=upload):
        result = hub._run_creator("run", "batch", AUTHOR, adapter)
    if resume:
        refresh.assert_not_called()
    return result, adapter, selected


@pytest.mark.parametrize("resume", [False, True])
def test_strict_cutoff_filters_before_transfer_and_database_including_recovery(hub, resume):
    stamp = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    videos = [observation("old-iso", "2026-08-31T15:59:59.999Z"),
              observation("old-seconds", stamp), observation("old-ms", stamp * 1000),
              observation("equal", "2026-09-01T00:00:00+08:00"),
              observation("new", "2026-10-01"), observation("raw-is-new", "2026-10-01", createtime=stamp),
              observation("unknown"), observation("invalid", "bad"), observation("bool", True)]
    result, adapter, selected = execute_creator(hub, videos, resume=resume)
    assert selected == ["old-iso", "old-seconds", "old-ms"]
    assert list(adapter.latest_rows.call_args.args[0]) == selected
    assert [row["source_video_key"] for row in adapter.write_snapshots.call_args.args[0]] == selected
    assert result["filtered_videos"] == 3
    assert result["database_written"] == 3
    assert result["failed_items"] == 3
    assert {f["error_type"] for f in result["failures"]} == {"UnknownPublishTime"}
    assert "不含当天" in result["message"]


def test_all_excluded_is_successful_without_uploading_or_writing_rows(hub):
    result, adapter, selected = execute_creator(hub, [observation("new", "2026-10-01")])
    assert selected == []
    assert adapter.write_snapshots.call_args.args[0] == []
    assert result["status"] == "completed" and result["failed_items"] == 0
    assert result["filtered_videos"] == 1
    assert "跳过 1 条" in result["message"]


def test_retry_can_fill_missing_publish_time_without_replacing_original_metrics(hub):
    old = observation("old")
    hub.journal.save_checkpoint("source-run", AUTHOR["username"], {"observations": [old]})
    hub.state["current_run"] = {"run_id": "run", "publish_filter": copy.deepcopy(SCOPE),
        "retry_source_run_id": "source-run", "retry_selection": {AUTHOR["username"]: {"ids": ["old"]}}}
    fresh = observation("old", "2026-08-01")
    fresh["rpa_payload"]["likeCount"] = 999
    adapter = MagicMock()
    adapter.latest_rows.return_value = {"old": {"video_url": "https://fixture.invalid/old.mp4"}}
    adapter.write_snapshots.side_effect = len
    with patch.object(hub, "_refresh_author", return_value=1), \
         patch.object(hub, "_load_author_videos", return_value=[fresh]), \
         patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, [])):
        result = hub._run_creator("run", "batch", AUTHOR, adapter)
    assert result["status"] == "completed"
    row = adapter.write_snapshots.call_args.args[0][0]
    assert row["published_at"] == datetime(2026, 8, 1)
    assert row["like_count"] == 7
