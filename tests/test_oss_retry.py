"""Failures captured from the September 14 download log at the real queue seam."""
from unittest.mock import Mock
import threading

import pytest
import requests

from backend import oss
from test_oss_concurrency import transfer, join


@pytest.mark.parametrize("failure", [requests.exceptions.ChunkedEncodingError("IncompleteRead"),
                                    requests.exceptions.ReadTimeout("source timeout")])
def test_stream_failure_restarts_entire_download_before_upload(transfer, monkeypatch, failure):
    t = transfer
    monkeypatch.setattr(oss, "TRANSFER_RETRY_DELAYS", (0, 0, 0), raising=False)
    monkeypatch.setattr(oss, "get_settings", lambda: {"download_dir": str(t.root)})
    monkeypatch.setattr(t.manager, "_download_video", oss.OSSUploadManager._download_video.__get__(t.manager))
    responses = []
    content = b"complete-video"

    def get(*args, **kwargs):
        response = Mock(headers={"content-length": str(len(content))})
        attempt = len(responses)
        def chunks(**_):
            if attempt == 0:
                yield b"bad-partial"
                raise failure
            yield content
        response.iter_content.side_effect = chunks
        responses.append(response)
        return response

    uploaded = []
    def upload(service, path, *args):
        uploaded.append(path.read_bytes())
        return t.upload(service, path, *args)

    monkeypatch.setattr(oss.requests, "get", get)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1],
                                  transfer={"download_workers": 1, "upload_workers": 1})
    join(t.manager)
    assert uploaded == [content]
    assert len(responses) == 2
    task = t.manager.tasks[0]
    assert task["status"] == "completed"
    assert task["download_attempts"] == 2
    assert task["attempt_errors"][0]["stage"] == "download"
    assert all(r.close.call_count == 1 for r in responses)


def test_retry_exhaustion_retains_cause_and_never_uploads_partial_file(transfer, monkeypatch):
    t = transfer
    monkeypatch.setattr(oss, "TRANSFER_RETRY_DELAYS", (0, 0, 0), raising=False)
    download = Mock(side_effect=requests.exceptions.ChunkedEncodingError("source truncated"))
    upload = Mock()
    monkeypatch.setattr(t.manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1],
                                  transfer={"download_workers": 1, "upload_workers": 1})
    join(t.manager)
    assert download.call_count == 4
    upload.assert_not_called()
    task = t.manager.tasks[0]
    assert task["status"] == "failed"
    assert task["stage"] == "download"
    assert task["error"] == "source truncated"
    assert len(task["attempt_errors"]) == 4


def test_short_body_even_without_library_error_is_not_uploaded(transfer, monkeypatch):
    t = transfer
    monkeypatch.setattr(oss, "get_settings", lambda: {"download_dir": str(t.root)})
    response = Mock(headers={"content-length": "50"})
    response.iter_content.return_value = iter([b"short"])
    monkeypatch.setattr(oss.requests, "get", Mock(return_value=response))
    with pytest.raises(Exception, match="不完整"):
        oss.OSSUploadManager._download_video(t.manager, "short", t.videos[0], isolated=True)
    assert not list((t.root / "channels" / "oss_cache").glob("short*"))


def test_pause_between_attempts_drains_then_resumes_without_new_requests(transfer, monkeypatch):
    t = transfer
    monkeypatch.setattr(oss, "TRANSFER_RETRY_DELAYS", (0, 0, 0), raising=False)
    resume, stop, acknowledged = threading.Event(), threading.Event(), threading.Event()
    resume.set()
    calls = []
    def download(task_id, video, **kwargs):
        calls.append(video["id"])
        if len(calls) == 1:
            resume.clear()
            raise requests.exceptions.ChunkedEncodingError("mid-stream disconnect")
        return t.download(task_id, video, **kwargs)
    def checkpoint():
        acknowledged.set()
        assert resume.wait(3)
    monkeypatch.setattr(t.manager, "_download_video", download)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1],
        transfer={"download_workers": 1, "upload_workers": 1}, resume_event=resume,
        stop_event=stop, pause_checkpoint=checkpoint)
    try:
        assert acknowledged.wait(3)
        assert len(calls) == 1
    finally:
        resume.set()
        join(t.manager)
    assert len(calls) == 2 and t.manager.tasks[0]["status"] == "completed"


def test_stop_cancels_pending_retry(transfer, monkeypatch):
    t = transfer
    stop = threading.Event()
    failed = threading.Event()
    monkeypatch.setattr(oss, "TRANSFER_RETRY_DELAYS", (10, 10, 10), raising=False)
    calls = []
    def download(*args, **kwargs):
        calls.append(1)
        failed.set()
        raise requests.exceptions.ReadTimeout("temporary")
    monkeypatch.setattr(t.manager, "_download_video", download)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1],
        transfer={"download_workers": 1, "upload_workers": 1}, stop_event=stop)
    assert failed.wait(3)
    stop.set()
    join(t.manager)
    assert calls == [1]
    assert t.manager.tasks[0]["status"] == "failed"


def test_permanent_http_error_is_not_retried(transfer, monkeypatch):
    t = transfer
    response = requests.Response()
    response.status_code = 403
    download = Mock(side_effect=requests.exceptions.HTTPError("forbidden", response=response))
    monkeypatch.setattr(t.manager, "_download_video", download)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1], transfer={"download_workers": 1, "upload_workers": 1})
    join(t.manager)
    assert download.call_count == 1
    assert t.manager.tasks[0]["error"] == "forbidden"


def test_upload_timeout_reuses_complete_local_file(transfer, monkeypatch):
    t = transfer
    monkeypatch.setattr(oss, "TRANSFER_RETRY_DELAYS", (0, 0, 0), raising=False)
    download = Mock(side_effect=t.download)
    uploaded = []
    def upload(service, path, *args):
        uploaded.append((str(path), path.read_bytes()))
        if len(uploaded) == 1:
            raise requests.exceptions.ReadTimeout("upload response lost")
        return t.upload(service, path, *args)
    monkeypatch.setattr(t.manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.manager.start_selected_sync({"username": "author-1"}, t.videos[:1], transfer={"download_workers": 1, "upload_workers": 1})
    join(t.manager)
    assert download.call_count == 1
    assert len(uploaded) == 2 and uploaded[0] == uploaded[1]
    assert t.manager.tasks[0]["status"] == "completed"
    assert not list(t.root.glob('*.mp4'))
