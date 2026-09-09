"""Exercise the real scheduler with controlled I/O and isolated local storage."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import channels, oss


AUTHOR = {"username": "author-1", "nickname": "作者"}


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(oss, "OSS_UPLOAD_TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(channels, "CHANNELS_FEEDS_FILE", tmp_path / "feeds.json")
    monkeypatch.setattr(oss, "get_oss_config", lambda **_: {
        "configured": True, "access_key_id": "test-user", "access_key_secret": "test-secret", "bucket": "test-bucket",
    })
    sessions = []

    def session():
        value = Mock()
        sessions.append(value)
        return value

    monkeypatch.setattr(oss.requests, "Session", session)
    manager = oss.OSSUploadManager()
    videos = [{"id": f"video-{i}", "video_url": f"https://source.invalid/{i}"} for i in range(20)]
    oss.save_json(channels.CHANNELS_FEEDS_FILE, {AUTHOR["username"]: videos})

    def download(task_id, video, **_):
        path = tmp_path / f"{task_id}.mp4"
        path.write_bytes(video["id"].encode())
        manager._update(task_id, status="downloading")
        return path

    def upload(service, path, video_id, created_at, callback):
        callback(10, 10)
        return {"size": 10, "url": f"https://oss.invalid/{video_id}.mp4",
                "object_key": f"{video_id}.mp4", "bucket": service.bucket}

    monkeypatch.setattr(manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    monkeypatch.setattr(oss.OSSService, "find_uploaded_video", lambda *args: None)
    context = SimpleNamespace(manager=manager, videos=videos, download=download, upload=upload,
                              sessions=sessions, root=tmp_path)
    yield context
    if manager.worker:
        manager.worker.join(5)
        assert not manager.worker.is_alive(), "OSS worker did not finish"


def join(manager):
    manager.worker.join(5)
    assert not manager.worker.is_alive()


@pytest.mark.parametrize("download_limit,upload_limit", [(1, 1), (3, 2), (2, 3)])
def test_independent_limits_overlap_and_bounded_backlog(transfer, monkeypatch, download_limit, upload_limit):
    t = transfer
    gate = threading.Event()
    downloads_full, uploads_full, overlap, upload_started = (threading.Event() for _ in range(4))
    lock = threading.Lock()
    counts = {"downloading": 0, "uploading": 0, "download_peak": 0, "upload_peak": 0, "downloaded": 0, "started": 0}
    first_wave = threading.Barrier(download_limit)
    owners = {}

    def download(task_id, video, **kwargs):
        with lock:
            counts["downloading"] += 1
            counts["started"] += 1
            counts["download_peak"] = max(counts["download_peak"], counts["downloading"])
            is_first = counts["started"] <= download_limit
            if counts["downloading"] == download_limit:
                downloads_full.set()
            if counts["uploading"]:
                overlap.set()
        if is_first:
            first_wave.wait(4)
        else:
            assert upload_started.wait(4)
            overlap.set()
        path = t.download(task_id, video, **kwargs)
        with lock:
            counts["downloading"] -= 1
            counts["downloaded"] += 1
        return path

    def upload(service, *args):
        with lock:
            owner = owners.setdefault(id(service.session), threading.get_ident())
            assert owner == threading.get_ident()
            counts["uploading"] += 1
            upload_started.set()
            counts["upload_peak"] = max(counts["upload_peak"], counts["uploading"])
            if counts["uploading"] == upload_limit:
                uploads_full.set()
        assert gate.wait(4)
        try:
            return t.upload(service, *args)
        finally:
            with lock:
                counts["uploading"] -= 1

    monkeypatch.setattr(t.manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.manager.start_selected_sync(AUTHOR, t.videos, transfer={"download_workers": download_limit, "upload_workers": upload_limit})
    try:
        assert downloads_full.wait(3)
        assert uploads_full.wait(3)
        assert overlap.wait(3), "downloads must continue while uploads are in flight"
        with lock:
            assert counts["downloaded"] <= download_limit + 2 * upload_limit
        with pytest.raises(RuntimeError, match="正在进行"):
            t.manager.start_selected_sync(AUTHOR, t.videos[:1])
    finally:
        gate.set()
        join(t.manager)
    assert counts["download_peak"] == download_limit
    assert counts["upload_peak"] == upload_limit
    assert all(task["status"] == "completed" for task in t.manager.tasks)
    saved = oss.load_json(channels.CHANNELS_FEEDS_FILE)[AUTHOR["username"]]
    assert len([v for v in saved if v.get("oss_video_url")]) == len(t.videos)
    assert not list(t.root.glob("*.mp4"))
    assert all(s.close.call_count == 1 for s in t.sessions)


def test_pause_drains_current_requests_and_resume_keeps_queue(transfer, monkeypatch):
    t = transfer
    resume, acknowledged, release_download, download_started = (threading.Event() for _ in range(4))
    resume.set()
    calls = []
    uploads = []

    def download(task_id, video, **kwargs):
        calls.append(video["id"])
        download_started.set()
        assert release_download.wait(4)
        return t.download(task_id, video, **kwargs)

    def upload(service, *args):
        uploads.append(args[1])
        return t.upload(service, *args)

    def checkpoint():
        acknowledged.set()
        assert resume.wait(4)

    monkeypatch.setattr(t.manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.manager.start_selected_sync(AUTHOR, t.videos[:4], transfer={"download_workers": 1, "upload_workers": 2},
                                  resume_event=resume, pause_checkpoint=checkpoint)
    try:
        assert download_started.wait(3)
        resume.clear()
        assert not acknowledged.is_set()
        release_download.set()
        assert acknowledged.wait(3)
        assert calls == ["video-0"]
        assert uploads == []
        assert t.manager.snapshot()["running"]
    finally:
        resume.set()
        release_download.set()
        join(t.manager)
    assert len(calls) == len(uploads) == 4
    assert all(task["status"] == "completed" for task in t.manager.tasks)


def test_failures_and_remote_reuse_do_not_drop_other_results(transfer, monkeypatch):
    t = transfer
    downloads, uploads = [], []

    def find(service, video_id, *args):
        if video_id == "video-1":
            return {"url": "https://existing.invalid/1.mp4", "object_key": "existing.mp4", "bucket": "old-bucket"}

    def download(task_id, video, **kwargs):
        downloads.append(video["id"])
        if video["id"] == "video-2":
            raise OSError("download failed")
        return t.download(task_id, video, **kwargs)

    def upload(service, path, video_id, *args):
        uploads.append(video_id)
        if video_id == "video-3":
            raise OSError("upload failed")
        return t.upload(service, path, video_id, *args)

    monkeypatch.setattr(oss.OSSService, "find_uploaded_video", find)
    monkeypatch.setattr(t.manager, "_download_video", download)
    monkeypatch.setattr(oss.OSSService, "upload_video", upload)
    t.videos[0]["oss_video_url"] = "https://existing.invalid/0.mp4"
    t.manager.start_selected_sync(AUTHOR, t.videos[:6], transfer={"download_workers": 3, "upload_workers": 2}, reuse_remote=True)
    join(t.manager)
    states = {task["video_id"]: task["status"] for task in t.manager.tasks}
    assert states == {"video-0": "skipped", "video-1": "skipped", "video-2": "failed",
                      "video-3": "failed", "video-4": "completed", "video-5": "completed"}
    assert set(downloads) == {"video-2", "video-3", "video-4", "video-5"}
    assert set(uploads) == {"video-3", "video-4", "video-5"}
    saved = oss.load_json(channels.CHANNELS_FEEDS_FILE)[AUTHOR["username"]]
    assert saved[1]["oss_video_url"] == "https://existing.invalid/1.mp4"
    assert saved[4]["oss_video_url"].endswith("video-4.mp4")
    assert not list(t.root.glob("*.mp4"))


def test_stop_while_paused_ends_queue_and_active_batch_records_cannot_be_cleared(transfer):
    t = transfer
    resume, stop, acknowledged = threading.Event(), threading.Event(), threading.Event()

    def checkpoint():
        acknowledged.set()
        stop.wait(4)

    t.manager.start_selected_sync(AUTHOR, t.videos[:3], transfer={"download_workers": 2, "upload_workers": 2},
                                  resume_event=resume, stop_event=stop, pause_checkpoint=checkpoint)
    try:
        assert acknowledged.wait(3)
        t.manager._update(t.manager.tasks[0]["id"], status="skipped")
        t.manager.clear_finished()
        assert len(t.manager.tasks) == 3
    finally:
        stop.set()
        join(t.manager)
    assert all(task["status"] == "failed" for task in t.manager.tasks)
    t.manager.clear_finished()
    assert not t.manager.tasks


def test_download_cache_is_isolated_and_decryption_failure_is_not_cached(transfer, monkeypatch):
    t = transfer
    monkeypatch.setattr(oss, "get_settings", lambda: {"download_dir": str(t.root)})
    responses = []

    def response(*args, **kwargs):
        value = Mock(headers={"content-length": "8"})
        value.iter_content.return_value = iter([b"testdata"])
        responses.append(value)
        return value

    monkeypatch.setattr(oss.requests, "get", response)
    first = oss.OSSUploadManager._download_video(t.manager, "task-a", t.videos[0], isolated=True)
    second = oss.OSSUploadManager._download_video(t.manager, "task-b", t.videos[0], isolated=True)
    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"testdata"
    monkeypatch.setattr(channels, "decrypt_channels_data", Mock(side_effect=ValueError("decrypt failed")))
    with pytest.raises(ValueError, match="decrypt failed"):
        oss.OSSUploadManager._download_video(t.manager, "task-c", {**t.videos[0], "decode_key": "123"}, isolated=True)
    cache = t.root / "channels" / "oss_cache"
    assert not list(cache.glob("task-c*"))
    assert all(response.close.call_count == 1 for response in responses)
