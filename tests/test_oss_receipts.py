from backend.channels_storage import read_feeds, feed_store, FeedStore
"""Reinstall regression through the real queue, uploader and receipt persistence."""
from types import SimpleNamespace
import pytest

from backend import channels, oss


AUTHOR = {"username": "author-1", "nickname": "作者"}
VIDEO = {"id": "video-1", "createtime": 1721174400, "video_url": "https://source.invalid/1"}
MP4 = b"\x00\x00\x00\x20ftypisom" + b"x" * 20


def test_migration_does_not_block_upload_status(sync, monkeypatch):
    import threading
    manager = sync.install('migration-slow')
    entered, release = threading.Event(), threading.Event()
    original = oss.read_feeds
    def slow_read(path):
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(oss,'read_feeds',slow_read)
    worker = threading.Thread(target=manager.migrate_upload_receipts)
    worker.start()
    try:
        assert entered.wait(2)
        assert manager.lock.acquire(timeout=0.2), 'Status polling must not wait for migration'
        manager.lock.release()
        assert manager.snapshot()['running'] is False
    finally:
        release.set()
        worker.join(5)
    assert manager._receipts_migrated


@pytest.fixture
def sync(tmp_path, monkeypatch):
    oss.save_oss_config("test-user", "test-secret", "test-bucket")
    calls = []
    state = SimpleNamespace(head_size=len(MP4))

    class Session:
        def put(self, url, data, **kwargs):
            calls.append(("PUT", url))
            assert data.read() == MP4
            return SimpleNamespace(status_code=200)

        def head(self, url, **kwargs):
            calls.append(("HEAD", url))
            return SimpleNamespace(status_code=200, headers={"Content-Length": str(state.head_size)})

        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return SimpleNamespace(status_code=206)

        def close(self):
            pass

    monkeypatch.setattr(oss.requests, "Session", Session)

    def install(name, videos=None, tasks=None):
        root = tmp_path / name
        monkeypatch.setattr(oss, "OSS_UPLOAD_TASKS_FILE", root / "tasks.json")
        monkeypatch.setattr(channels, "CHANNELS_FEEDS_FILE", root / "feeds.json")
        oss.save_json(channels.CHANNELS_FEEDS_FILE, {AUTHOR["username"]: videos or [dict(VIDEO)]})
        if tasks is not None:
            oss.save_json(oss.OSS_UPLOAD_TASKS_FILE, tasks)
        manager = oss.OSSUploadManager()

        def download(task_id, video, **kwargs):
            calls.append(("DOWNLOAD", video["id"]))
            path = root / f"{task_id}.mp4"
            path.write_bytes(MP4)
            return path

        monkeypatch.setattr(manager, "_download_video", download)
        return manager

    def run(manager, parallel=False, videos=None, **kwargs):
        options = {"transfer": {"download_workers": 2, "upload_workers": 2}} if parallel else {}
        result = manager.start_selected_sync(AUTHOR, videos or [dict(VIDEO)], **options, **kwargs)
        manager.worker.join(5)
        assert not manager.worker.is_alive()
        return manager.snapshot(result["batch_id"])["items"]

    return SimpleNamespace(install=install, run=run, calls=calls, state=state)


@pytest.mark.parametrize("parallel", [False, True])
def test_reinstall_and_clear_history_do_not_repeat_download_or_upload(sync, parallel):
    first = sync.install("install-old")
    original = sync.run(first, parallel)[0]
    assert original["status"] == "completed"
    assert [c[0] for c in sync.calls] == ["DOWNLOAD", "PUT", "HEAD", "GET"]
    first.clear_finished()
    assert not first.tasks

    second = sync.install("install-new")
    sync.calls.clear()
    restored = sync.run(second, parallel)[0]
    assert sync.calls == [], "The same verified video must not be downloaded or uploaded again after reinstall"
    assert restored["status"] == "skipped"
    assert restored["oss_url"] == original["oss_url"]
    saved = read_feeds(channels.CHANNELS_FEEDS_FILE)[AUTHOR["username"]][0]
    assert saved["oss_video_url"] == original["oss_url"]


@pytest.mark.parametrize("parallel", [False, True])
def test_fresh_capture_without_oss_fields_reuses_history_in_same_install(sync, parallel):
    manager = sync.install("install")
    assert sync.run(manager, parallel)[0]["status"] == "completed"
    sync.calls.clear()
    # Same installation, but a fresh capture carries only source metadata.
    assert sync.run(manager, parallel)[0]["status"] == "skipped"
    assert sync.calls == []


@pytest.mark.parametrize("parallel", [False, True])
def test_missing_creation_date_does_not_invalidate_success_for_same_video(sync, parallel):
    first = sync.install("old")
    original = sync.run(first, parallel)[0]
    second = sync.install("new")
    sync.calls.clear()
    result = sync.run(second, parallel, videos=[{**VIDEO, "createtime": ""}])[0]
    assert result["oss_url"] == original["oss_url"]
    assert result["status"] == "skipped"
    assert sync.calls == []


@pytest.mark.parametrize("parallel", [False, True])
def test_switching_bucket_cannot_reuse_other_buckets_receipt(sync, parallel):
    first = sync.install("old")
    original = sync.run(first, parallel)[0]
    oss.save_oss_config("test-user", "test-secret", "other-bucket")
    second = sync.install("new")
    sync.calls.clear()
    result = sync.run(second, parallel)[0]
    assert result["status"] == "completed"
    assert result["oss_url"] != original["oss_url"]
    assert "/other-bucket/" in result["oss_url"]
    assert [c[0] for c in sync.calls] == ["DOWNLOAD", "PUT", "HEAD", "GET"]


@pytest.mark.parametrize("parallel", [False, True])
def test_failed_remote_verification_never_becomes_a_success_receipt(sync, parallel):
    first = sync.install("old")
    sync.state.head_size = 0
    assert sync.run(first, parallel)[0]["status"] == "failed"
    second = sync.install("new")
    sync.calls.clear()
    sync.state.head_size = len(MP4)
    assert sync.run(second, parallel)[0]["status"] == "completed"
    assert [c[0] for c in sync.calls] == ["DOWNLOAD", "PUT", "HEAD", "GET"]


@pytest.mark.parametrize("parallel", [False, True])
def test_verified_upload_survives_feed_write_failure(sync, monkeypatch, parallel):
    first = sync.install("old")
    original_write = FeedStore.patch_existing

    def fail_feed(*args):
        raise OSError("simulated capture disk failure")

    monkeypatch.setattr(FeedStore, "patch_existing", fail_feed)
    failed = sync.run(first, parallel)[0]
    assert failed["status"] == "failed"
    assert "simulated capture disk failure" in failed["error"]
    monkeypatch.setattr(FeedStore, "patch_existing", original_write)
    second = sync.install("new")
    sync.calls.clear()
    restored = sync.run(second, parallel)[0]
    assert restored["status"] == "skipped"
    assert sync.calls == []


@pytest.mark.parametrize("source", ["feed", "task"])
def test_old_successes_are_migrated_before_any_new_sync_or_history_clear(sync, source):
    key = oss.build_oss_object_key(VIDEO["id"], VIDEO["createtime"])
    url = oss.build_oss_public_url(key, "test-bucket")
    # Legacy records can have a URL but no explicit bucket or object key.
    if source == "feed":
        first = sync.install("old", videos=[{**VIDEO, "oss_video_url": url, "oss_upload_status": "completed"}])
        first.migrate_upload_receipts()  # The application startup migration.
    else:
        first = sync.install("old", tasks=[{"video_id": VIDEO["id"], "oss_url": url, "status": "completed"}])
        first.clear_finished()
    second = sync.install("new")
    restored = sync.run(second)[0]
    assert sync.calls == []
    assert restored["status"] == "skipped"
    assert restored["oss_url"] == url


@pytest.mark.parametrize("bad_record", [
    {"status": "failed"},
    {"status": "uploading"},
    {"oss_bucket": "wrong-bucket"},
    {"oss_url": "https://unrelated.invalid/test-bucket/wechat_channel/video-1.mp4"},
    {"oss_url": "https://oss.fandow.com/test-bucket/wechat_channel/other-video.mp4"},
])
def test_invalid_or_failed_legacy_tasks_are_not_reused(sync, bad_record):
    url = oss.build_oss_public_url(oss.build_oss_object_key(VIDEO["id"]), "test-bucket")
    first = sync.install("old", tasks=[{"video_id": VIDEO["id"], "oss_url": url,
                                       "status": "completed", **bad_record}])
    first.migrate_upload_receipts()
    second = sync.install("new")
    assert sync.run(second)[0]["status"] == "completed"
    assert [c[0] for c in sync.calls] == ["DOWNLOAD", "PUT", "HEAD", "GET"]


def test_old_migration_does_not_overwrite_a_newer_verified_object(sync):
    original = sync.run(sync.install("original"))[0]
    old_url = oss.build_oss_public_url(oss.build_oss_object_key(VIDEO["id"]), "test-bucket")
    manager = sync.install("legacy", tasks=[{"video_id": VIDEO["id"], "oss_url": old_url, "status": "completed"}])
    sync.calls.clear()
    restored = sync.run(manager)[0]
    assert sync.calls == []
    assert restored["oss_url"] == original["oss_url"] != old_url


def test_remote_reuse_populates_the_same_persistent_cache(sync, monkeypatch):
    first = sync.install("old")
    key = oss.build_oss_object_key(VIDEO["id"], VIDEO["createtime"])
    remote = {"url": oss.build_oss_public_url(key, "test-bucket"), "bucket": "test-bucket",
              "object_key": key, "size": len(MP4)}
    probes = []

    def find(*args):
        probes.append(args)
        return remote

    monkeypatch.setattr(oss.OSSService, "find_uploaded_video", find)
    assert sync.run(first, parallel=True, reuse_remote=True)[0]["status"] == "skipped"
    assert len(probes) == 1
    assert sync.calls == []
    second = sync.install("new")
    assert sync.run(second, parallel=True, reuse_remote=True)[0]["status"] == "skipped"
    assert len(probes) == 1, "A local hit must also avoid repeat remote probes"
    assert sync.calls == []


def test_receipts_are_scoped_to_endpoint_and_prefix(sync, monkeypatch):
    sync.run(sync.install("old"))
    monkeypatch.setattr(oss, "DEFAULT_OSS_ENDPOINT", "https://other-oss.invalid")
    assert sync.run(sync.install("new-endpoint"))[0]["status"] == "completed"
    monkeypatch.setattr(oss, "DEFAULT_OSS_OBJECT_PREFIX", "other-prefix")
    assert sync.run(sync.install("new-prefix"))[0]["status"] == "completed"
    assert [c[0] for c in sync.calls].count("PUT") == 3
