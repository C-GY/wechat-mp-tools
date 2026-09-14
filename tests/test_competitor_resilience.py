from datetime import datetime
import time
from unittest.mock import MagicMock, patch

import pymysql
import pytest

from backend import competitor_monitor as monitor
from backend.competitor_monitor_store import CompetitorMySQLAdapter


@pytest.fixture
def hub(tmp_path):
    return monitor.CompetitorMonitorHub(tmp_path / "config.json", tmp_path / "state.json")


AUTHOR = {"username": "author-1", "nickname": "作者"}


def observe(hub, count=1, error="Connection broken: IncompleteRead", reported=None):
    videos = [{"id": str(i), "collected_at": time.time() + 10, "rpa_payload": {"id": str(i)}} for i in range(count)]
    adapter = MagicMock()
    adapter.latest_rows.return_value = {}
    adapter.write_snapshots.side_effect = len
    failures = [{"video_id": v["id"], "error": error, "stage": "download", "error_type": "ChunkedEncodingError"} for v in videos]
    with patch.object(hub, "_refresh_author", return_value=reported if reported is not None else count), patch.object(hub, "_load_author_videos", return_value=videos), patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, failures)):
        return hub._run_creator("run-1", "batch-1", AUTHOR, adapter)


def test_original_download_error_survives_row_validation(hub):
    result = observe(hub)
    assert result["failures"][0]["error"] == "Connection broken: IncompleteRead"
    assert result["failures"][0]["stage"] == "download"
    assert result["database_written"] == 0


def test_more_than_100_failures_remain_available(hub):
    result = observe(hub, 103)
    assert result["failed_items"] == len(result["failures"]) == 103


def test_partial_capture_cannot_be_reported_as_complete(hub):
    videos = [{"id": "1", "collected_at": time.time() + 10, "oss_video_url": "https://fixture.invalid/1.mp4"}]
    adapter = MagicMock()
    adapter.latest_rows.return_value = {"1": {"video_url": videos[0]["oss_video_url"]}}
    adapter.write_snapshots.side_effect = len
    with patch.object(hub, "_refresh_author", return_value=2), patch.object(hub, "_load_author_videos", return_value=videos), patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, [])):
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert result["status"] != "completed"
    assert any(f["stage"] == "capture" for f in result["failures"])


def test_invalid_video_identity_does_not_discard_valid_observations(hub):
    videos = [{"id": "good", "collected_at": time.time() + 10},
              {"description": "missing identity", "collected_at": time.time() + 10}]
    adapter = MagicMock()
    adapter.latest_rows.return_value = {"good": {"video_url": "https://fixture.invalid/good.mp4"}}
    adapter.write_snapshots.side_effect = len
    with patch.object(hub, "_refresh_author", return_value=2), patch.object(hub, "_load_author_videos", return_value=videos), patch.object(hub, "_sync_new_videos_to_oss", return_value=(0, [])):
        result = hub._run_creator("run-1", "batch-1", AUTHOR, adapter)
    assert result["status"] == "partial"
    assert result["database_written"] == 1 and result["failed_items"] == 1
    assert result["failures"][0]["stage"] == "validation"


def test_database_rollback_failure_does_not_replace_query_failure():
    connection = pymysql.connections.Connection(defer_connect=True)
    cursor = MagicMock()
    cursor.execute.side_effect = pymysql.err.OperationalError(2013, "lost connection during query")
    connection.cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    adapter = CompetitorMySQLAdapter({})
    row = {"platform": "wechat_channels", "source_video_key": "fixture", "synced_at": datetime(2026, 9, 14), "sync_batch_id": "batch"}
    with patch.object(adapter, "connect", return_value=connection):
        with pytest.raises(pymysql.err.OperationalError, match="2013"):
            adapter.write_snapshots([row])


def test_database_read_reconnects_without_consuming_iterator(monkeypatch):
    from backend import database_retry
    monkeypatch.setattr(database_retry, "DATABASE_RETRY_DELAYS", (0, 0))
    first, second = MagicMock(), MagicMock()
    first.cursor.return_value.__enter__.return_value.execute.side_effect = pymysql.err.OperationalError(2013, "disconnected")
    second.cursor.return_value.__enter__.return_value.fetchall.return_value = [{"source_video_key": "video-1", "video_url": "https://fixture.invalid/1.mp4"}]
    adapter = CompetitorMySQLAdapter({})
    with patch.object(adapter, "connect", side_effect=[first, second]) as connect:
        result = adapter.latest_rows(iter(["video-1"]))
    assert result["video-1"]["video_url"].endswith("1.mp4")
    assert connect.call_count == 2
    assert second.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == ["wechat_channels", "video-1"]
    first.close.assert_called_once()
    second.close.assert_called_once()


def test_database_auth_error_is_not_retried():
    adapter = CompetitorMySQLAdapter({})
    with patch.object(adapter, "connect", side_effect=pymysql.err.OperationalError(1045, "access denied")) as connect:
        with pytest.raises(pymysql.err.OperationalError, match="1045"):
            adapter.latest_rows(["video-1"])
    connect.assert_called_once()


def test_database_write_replays_whole_operation_after_commit_response_lost(monkeypatch):
    from backend import database_retry
    from backend.competitor_monitor_store import build_row
    monkeypatch.setattr(database_retry, "DATABASE_RETRY_DELAYS", (0, 0))
    first, second = MagicMock(), MagicMock()
    now = datetime(2026, 9, 14)
    row = build_row(AUTHOR, {"id": "fixture-video", "oss_video_url": "https://fixture.invalid/1.mp4"}, "stable-batch", synced_at=now)
    for connection in (first, second):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.lastrowid = 1
        cursor.fetchone.side_effect = [{"last_synced_at": now, "video_url": row["video_url"]}, None]
    first.commit.side_effect = pymysql.err.OperationalError(2013, "commit response lost")
    first.rollback.side_effect = pymysql.err.InterfaceError(0, "")
    adapter = CompetitorMySQLAdapter({})
    with patch.object(adapter, "connect", side_effect=[first, second]) as connect:
        assert adapter.write_snapshots([row]) == 1
    assert connect.call_count == 2
    first.commit.assert_called_once()
    second.commit.assert_called_once()
    assert first.cursor.return_value.__enter__.return_value.execute.call_args_list == second.cursor.return_value.__enter__.return_value.execute.call_args_list
