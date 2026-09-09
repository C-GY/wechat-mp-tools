import copy
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from backend import competitor_monitor as monitor, pinchuang
from backend.competitor_monitor_store import build_row, source_key


AUTHOR = {"username": "author-1", "nickname": "作者一"}


def video(**changes):
    return {"id": "video-1", "description": "作品 #品牌 #营销", "oss_video_url": "https://oss.example/video.mp4",
            "like_count": 12, "share_count": 3, "favorite_count": 4, "comment_count": 0, **changes}


def test_mapping_preserves_raw_metrics_precision_tags_and_beijing_time():
    raw = {"id": "video-1", "createtime": "2026-09-08T01:02:03.456Z", "likeCount": "9007199254740993",
           "forwardCount": "1.2万", "favCount": 0, "commentCount": None,
           "contact": {"username": "author-1", "nickname": "最新名字"},
           "objectDesc": {"description": "作品 #品牌 #营销", "media": [{"videoPlayLen": 125.8, "coverUrl": "https://img.example/1.jpg"}]}}
    v = video(rpa_payload=raw, tags=["品牌", {"name": "测评"}], collected_at=1788836523.987)
    row = build_row(AUTHOR, v, "batch-1")
    assert [row[k] for k in ("like_count", "share_count", "favorite_count", "comment_count")] == [9007199254740993, 12000, 0, None]
    assert row["duration_ms"] == 125800
    assert row["published_at"] == datetime(2026, 9, 8, 9, 2, 3, 456000)
    assert row["last_synced_at"] == row["synced_at"]
    assert row["synced_at"].tzinfo is None
    assert row["author_name"] == "最新名字"
    assert row["tags"] == ["品牌", "测评", "营销"]
    assert json.loads(row["raw_payload"])["video"]["rpa_payload"] == raw


@pytest.mark.parametrize("value,expected", [(None, None), (0, 0), ("--", None), ("1万+", None), (float("inf"), None), ("-3", None), (True, None), (str(2**64 - 1), 2**64 - 1)])
def test_unknown_counts_are_not_zero_and_bigints_are_exact(value, expected):
    # NaN/Infinity cannot be serialized to MySQL JSON, so exercise parsing alone for those.
    from backend.competitor_monitor_store import _number
    assert _number(value) == expected


@pytest.mark.parametrize("changes", [{"id": "a" * 129}, {"oss_video_url": "https://oss.example/" + "a" * 2048}, {"tags": ["字" * 129]}, {"like_count": str(2**64)}, {"duration_seconds": 2**32}])
def test_rejects_truncated_identity_urls_tags_and_unsigned_overflow(changes):
    with pytest.raises(ValueError):
        build_row(AUTHOR, video(**changes), "batch-1")


def test_url_identity_and_existing_oss_url_are_stable():
    first = {"share_url": "https://EXAMPLE.com/video?b=2&a=1#one"}
    second = {"share_url": "https://example.com/video?a=1&b=2#two"}
    assert source_key(first) == source_key(second)
    row = build_row(AUTHOR, video(), "batch-1", existing_video_url="https://stored.example/1.mp4",
                    synced_at=datetime(2026, 9, 8, 1, tzinfo=timezone.utc))
    assert row["video_url"] == "https://stored.example/1.mp4"
    assert row["synced_at"] == datetime(2026, 9, 8, 9)


def test_fractional_seconds_round_to_millisecond_precision():
    assert build_row(AUTHOR, video(duration_seconds=150.333333), "batch")["duration_ms"] == 150333
    assert build_row(AUTHOR, video(duration_seconds=1.2345), "batch")["duration_ms"] == 1235


@pytest.fixture
def hub(tmp_path):
    return monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")


def test_creator_uses_only_this_refresh_and_keeps_observed_counts_during_upload(hub):
    observed = video(collected_at=time.time() + 1, rpa_payload={"likeCount": 15, "favCount": 0})
    refreshed_later = {**observed, "rpa_payload": {"likeCount": 999}, "oss_video_url": "https://oss.example/uploaded.mp4"}
    stale = video(id="deleted-video", collected_at=1)
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = lambda rows: len(rows)
    with patch.object(hub, "_refresh_author", return_value=1), patch.object(hub, "_load_author_videos", side_effect=[[observed, stale], [refreshed_later, stale]]), patch.object(hub, "_sync_new_videos_to_oss", return_value=(1, [])):
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert result["failed_items"] == 0
    assert result["database_written"] == 1
    rows = adapter.write_snapshots.call_args.args[0]
    assert rows[0]["like_count"] == 15
    assert rows[0]["comment_count"] is None
    assert rows[0]["video_url"] == "https://oss.example/uploaded.mp4"
    assert list(adapter.latest_rows.call_args.args[0]) == ["video-1"]


def test_stale_cache_cannot_be_reported_as_a_successful_refresh(hub):
    adapter = MagicMock()
    with patch.object(hub, "_refresh_author", return_value=1), patch.object(hub, "_load_author_videos", return_value=[video()]):
        with pytest.raises(RuntimeError, match="没有本轮采集原文"):
            hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    adapter.write_snapshots.assert_not_called()


def test_creator_reuses_master_oss_url_and_reports_bad_items(hub):
    videos = [video(collected_at=time.time() + 1), video(id="bad-video", collected_at=time.time() + 1, tags=["x" * 129])]
    adapter = MagicMock()
    adapter.latest_rows.return_value = {v["id"]: {"video_url": "https://stored.example/1.mp4"} for v in videos}
    adapter.write_snapshots.side_effect = lambda rows: len(rows)
    with patch.object(hub, "_refresh_author", return_value=2), patch.object(hub, "_load_author_videos", return_value=videos), patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, [])) as upload:
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert upload.call_args.args[2] == []
    assert result["status"] == "partial"
    assert result["failed_items"] == result["database_written"] == 1
    assert adapter.write_snapshots.call_args.args[0][0]["video_url"] == "https://stored.example/1.mp4"


def test_missing_platform_id_uses_safe_upload_key_without_inventing_external_id(hub):
    import hashlib

    observed = video(id="", share_url="https://channels.example/video?object=1", collected_at=time.time() + 1)
    key, _ = source_key(observed)
    upload_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
    manager = MagicMock()
    manager.start_selected_sync.return_value = {"batch_id": "upload-1"}
    manager.snapshot.return_value = {"items": [{"batch_id": "upload-1", "video_id": upload_id,
                                                "status": "completed", "oss_url": "https://oss.example/hashed.mp4"}]}
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = lambda rows: len(rows)
    with patch.object(hub, "_refresh_author", return_value=1), patch.object(hub, "_load_author_videos", return_value=[observed]), patch.object(monitor, "upload_manager", manager):
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert result["status"] == "completed"
    row = adapter.write_snapshots.call_args.args[0][0]
    assert row["source_video_key"] == key
    assert row["external_video_id"] is None
    assert row["video_url"] == "https://oss.example/hashed.mp4"
    assert observed["id"] == ""


def test_existing_remote_video_is_reused_when_local_upload_record_is_missing(hub):
    manager = MagicMock()
    manager.start_selected_sync.return_value = {"batch_id": "upload-1"}
    manager.snapshot.return_value = {"items": [{"batch_id": "upload-1", "video_id": "video-1", "status": "skipped", "oss_url": "https://oss.example/existing.mp4"}]}
    with patch.object(monitor, "upload_manager", manager):
        assert hub._sync_new_videos_to_oss("run-1", AUTHOR, [video(oss_video_url="")]) == (1, [])
    options = manager.start_selected_sync.call_args.kwargs
    assert options["reuse_remote"] is True
    assert options["transfer"] == {"download_workers": 2, "upload_workers": 2}
    assert options["resume_event"] is hub.resume_event
    assert options["stop_event"] is hub.stop_event
    assert hub._uploaded_video_urls["video-1"] == "https://oss.example/existing.mp4"


def test_independent_config_api_backup_schedule_and_pause_resume(hub):
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    with patch.object(monitor, "competitor_monitor_hub", hub), patch.object(pinchuang, "get_oss_config", return_value={"configured": True}):
        client = app.test_client()
        cfg = client.get("/api/competitor_monitor/config").json
        assert cfg["database"]["database"] == "competitor_monitor"
        assert cfg["tables"] == ["competitor_videos", "competitor_video_tags", "competitor_video_snapshots"]
        assert not cfg["schedule"]["enabled"]
        saved = client.post("/api/competitor_monitor/config", json={"database": {"host": "localhost", "username": "writer", "password": "fixture-secret"}, "schedule": {"times": ["09:00", "18:00"], "enabled": True}})
        assert saved.status_code == 200
        assert "password" not in saved.json["database"]
        backup = client.post("/api/competitor_monitor/config/export")
        assert backup.headers["Cache-Control"] == "no-store"
        assert backup.json["config"]["database"]["password"] == "fixture-secret"
        wrong = copy.deepcopy(backup.json)
        wrong["format"] = "pinchuang_config"
        assert client.post("/api/competitor_monitor/config/import", json=wrong).status_code == 400
        assert client.post("/api/competitor_monitor/config/import", json=backup.json).status_code == 200
        with patch.object(hub, "_database_adapter") as adapter:
            adapter.return_value.test_connection.return_value = {"version": "8.0", "tables": cfg["tables"]}
            assert client.post("/api/competitor_monitor/test-database").status_code == 200
        with patch.object(pinchuang.threading.Thread, "start"):
            run = client.post("/api/competitor_monitor/runs")
        assert run.status_code == 202
        hub.worker = MagicMock()
        hub.worker.is_alive.return_value = True
        assert client.post("/api/competitor_monitor/runs").status_code == 409
        assert client.post("/api/competitor_monitor/runs/pause").json["status"] == "pausing"
        assert client.post("/api/competitor_monitor/runs/resume").json["sync_batch_id"] == run.json["sync_batch_id"]
        hub.worker = None
        hub._finish_run(run.json["run_id"], "completed", "fixture")
        hub.now = lambda: datetime(2026, 9, 8, 9, tzinfo=pinchuang.BEIJING_TZ)
        with patch.object(hub, "start_run") as start:
            hub._scheduler_tick()
            hub._scheduler_tick()
            start.assert_called_once_with(trigger="scheduled", scheduled_time="09:00")
        assert monitor.CONFIG_FILE != pinchuang.PINCHUANG_CONFIG_FILE


@pytest.mark.parametrize("key", ["download_workers", "upload_workers"])
@pytest.mark.parametrize("value", [0, 9, -1, 1.5, True, None, "2", [], {}])
def test_transfer_config_rejects_invalid_limits_without_changing_saved_settings(hub, key, value):
    hub.save_config({"transfer": {"download_workers": 3, "upload_workers": 4}})
    before = hub.config_path.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    with patch.object(monitor, "competitor_monitor_hub", hub):
        response = app.test_client().post("/api/competitor_monitor/config", json={"transfer": {key: value}})
    assert response.status_code == 400
    assert "1 到 8" in response.json["error"]
    assert hub.config_path.read_bytes() == before


def test_transfer_settings_round_trip_and_legacy_backup_defaults(hub):
    assert hub.get_config()["transfer"] == monitor.DEFAULT_TRANSFER
    result = hub.save_config({"transfer": {"download_workers": 8, "upload_workers": 1}})
    assert result["transfer"] == {"download_workers": 8, "upload_workers": 1}
    hub.save_config({"schedule": {"creator_interval_seconds": 15}})
    backup = hub.export_config_backup()
    assert backup["config"]["transfer"] == result["transfer"]
    hub.save_config({"transfer": {"upload_workers": 5}})
    hub.import_config_backup(backup)
    restarted = monitor.CompetitorMonitorHub(hub.config_path, hub.state_path)
    assert restarted.config["transfer"] == result["transfer"]
    legacy = copy.deepcopy(backup)
    del legacy["config"]["transfer"]
    hub.import_config_backup(legacy)
    assert hub.get_config()["transfer"] == monitor.DEFAULT_TRANSFER
    hub.import_config_backup(legacy["config"])
    assert hub.config["transfer"] == monitor.DEFAULT_TRANSFER
    assert "transfer" not in pinchuang.DEFAULT_CONFIG


def test_running_transfer_limits_are_frozen_and_monitor_waits_for_queue_shutdown(hub):
    hub.config["transfer"] = {"download_workers": 3, "upload_workers": 1}
    hub.state["current_run"] = {"run_id": "run-1"}
    with patch.object(pinchuang.PinchuangHub, "_run_pipeline"):
        hub._run_pipeline("run-1")
        hub.save_config({"transfer": {"download_workers": 8, "upload_workers": 8}})
        hub._run_pipeline("run-1")  # Recovery must retain the same limits.
    assert hub.state["current_run"]["transfer"] == {"download_workers": 3, "upload_workers": 1}
    manager = MagicMock()
    manager.start_selected_sync.return_value = {"batch_id": "upload-1"}
    tasks = [{"batch_id": "upload-1", "video_id": "video-1", "status": "completed", "oss_url": "https://oss.invalid/1.mp4"}]
    manager.snapshot.side_effect = [{"items": tasks, "running": True, "batch_id": "upload-1"},
                                    {"items": tasks, "running": False, "batch_id": "upload-1"}]
    with patch.object(monitor, "upload_manager", manager), patch.object(monitor.time, "sleep") as sleep:
        assert hub._sync_new_videos_to_oss("run-1", AUTHOR, [video()]) == (1, [])
    assert manager.start_selected_sync.call_args.kwargs["transfer"] == {"download_workers": 3, "upload_workers": 1}
    sleep.assert_called_once()
