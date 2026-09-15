"""Replay confirmed progress so a healthy long capture is not timed out."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import channels_refresh, competitor_monitor, pinchuang


AUTHOR = {"username": "author"}


@pytest.fixture
def hub(tmp_path):
    channels_refresh.reset_refresh_task()
    yield competitor_monitor.CompetitorMonitorHub(tmp_path / 'config.json', tmp_path / 'state.json')
    channels_refresh.reset_refresh_task()


class Clock:
    seconds = 0

    def monotonic(self):
        return self.seconds

    def sleep(self, _):
        self.seconds += 30


def test_recorded_progress_can_continue_past_thirty_minutes(hub):
    progress = json.loads((Path(__file__).parent / 'fixtures/capture_progress_20260914.json').read_text())['progress']
    clock = Clock()
    get_status = channels_refresh.get_refresh_status

    def snapshot(task_id):
        # Replay successful storage receipts; append a synthetic completion
        # after minute 31 to assert the collector is allowed to reach it.
        count = max((p['saved_count'] for p in progress if p['seconds'] <= clock.seconds), default=0)
        if count:
            channels_refresh.record_capture(task_id, 'author', [str(i) for i in range(count)])
        if clock.seconds >= 1860:
            channels_refresh.update_refresh_task({'task_id': task_id, 'status': 'completed',
                'author_results': {'author': {'count': count, 'pagination_complete': True}}})
        return get_status(task_id)

    with patch.object(pinchuang.time, 'monotonic', clock.monotonic), \
         patch.object(pinchuang.time, 'sleep', clock.sleep), \
         patch.object(channels_refresh, 'get_refresh_status', snapshot), patch.object(hub, '_update_run'):
        assert hub._refresh_author('run', AUTHOR) == 1590
    assert clock.seconds == 1860


@pytest.mark.parametrize('duplicate_receipts', [False, True])
def test_heartbeats_and_duplicate_receipts_do_not_extend_stalled_capture(hub, duplicate_receipts):
    clock = Clock()
    get_status = channels_refresh.get_refresh_status

    def snapshot(task_id):
        channels_refresh.update_refresh_task({'task_id': task_id, 'status': 'running', 'total_videos': 999999})
        if duplicate_receipts:
            channels_refresh.record_capture(task_id, 'author', ['same-video'])
        return get_status(task_id)

    with patch.object(pinchuang.time, 'monotonic', clock.monotonic), \
         patch.object(pinchuang.time, 'sleep', clock.sleep), \
         patch.object(channels_refresh, 'get_refresh_status', snapshot), patch.object(hub, '_update_run'):
        with pytest.raises(channels_refresh.CaptureRefreshError) as caught:
            hub._refresh_author('run', AUTHOR)
    assert clock.seconds == 300
    assert caught.value.captured_count == int(duplicate_receipts)
    assert caught.value.capture_diagnostic['timeout']['reason'] == 'no_saved_progress'
    status = get_status()
    assert status['status'] == 'failed'
    assert channels_refresh.start_refresh_task([{'username': 'next'}])[1]


def test_public_progress_only_counts_confirmed_unique_ids():
    channels_refresh.reset_refresh_task()
    try:
        task, _ = channels_refresh.start_refresh_task([AUTHOR], require_receipt=True)
        channels_refresh.record_capture(task['task_id'], 'author', ['a', 'a', 'b'])
        status = channels_refresh.update_refresh_task({'task_id': task['task_id'], 'status': 'running',
            'persisted_counts': {'author': 999}, 'total_videos': 999})
        assert status['persisted_counts'] == {'author': 2}
        assert 'persisted_ids' not in status
        status['persisted_counts']['author'] = 100
        assert channels_refresh.get_refresh_status()['persisted_counts'] == {'author': 2}
    finally:
        channels_refresh.reset_refresh_task()


@pytest.mark.parametrize('terminal_status', ['completed', 'cancelled'])
def test_completion_or_cancellation_wins_race_with_timeout(hub, terminal_status):
    clock = Clock()
    update = channels_refresh.update_refresh_task

    def publish(payload):
        if payload.get('status') == 'failed':
            update({'task_id': payload['task_id'], 'status': terminal_status, 'message': terminal_status,
                    'author_results': {'author': {'count': 0, 'pagination_complete': True}}})
        return update(payload)

    with patch.object(pinchuang.time, 'monotonic', clock.monotonic), \
         patch.object(pinchuang.time, 'sleep', clock.sleep), \
         patch.object(channels_refresh, 'update_refresh_task', publish), patch.object(hub, '_update_run'):
        if terminal_status == 'completed':
            assert hub._refresh_author('run', AUTHOR) == 0
        else:
            with pytest.raises(channels_refresh.CaptureRefreshError) as caught:
                hub._refresh_author('run', AUTHOR)
            assert not caught.value.pagination_incomplete
            assert 'timeout' not in caught.value.capture_diagnostic
    assert channels_refresh.get_refresh_status()['status'] == terminal_status
