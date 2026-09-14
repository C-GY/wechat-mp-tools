"""Competitor monitoring with independent configuration and three-table storage."""

from __future__ import annotations

import hashlib
import copy
import re
import time

from flask import Blueprint, jsonify, request
import pymysql

from backend import pinchuang
from backend.competitor_recovery import CompetitorRecovery
from backend.sync_errors import error_details
from backend.competitor_author_tags import CompetitorAuthorTagStore
from backend.competitor_monitor_store import CompetitorMySQLAdapter, TABLES, build_row, source_key
from backend.oss import persistent_config_dir, upload_manager, validate_transfer_workers


competitor_monitor_bp = Blueprint("competitor_monitor", __name__, url_prefix="/api/competitor_monitor")
CONFIG_FILE = persistent_config_dir() / "competitor_monitor_config.json"
STATE_FILE = persistent_config_dir() / "competitor_monitor_state.json"
DEFAULT_DATABASE = "competitor_monitor"
DEFAULT_TRANSFER = {"download_workers": 2, "upload_workers": 2}


def _index_observations(videos):
    indexed = {}
    for index, video in enumerate(videos):
        try:
            key = source_key(video)[0]
        except ValueError:
            # Keep malformed items for per-video validation below. One bad
            # identity must not prevent valid observations from being saved.
            key = ("invalid", index)
        indexed[key] = video
    return indexed


def merged_config(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    database = raw.get("database") if isinstance(raw.get("database"), dict) else {}
    result = pinchuang._merged_config({
        **raw,
        "database": {"database": DEFAULT_DATABASE, **database},
    })
    transfer = raw.get("transfer") if isinstance(raw.get("transfer"), dict) else {}
    result["transfer"] = {**DEFAULT_TRANSFER, **transfer}
    return result


def normalize_config(payload: dict, current: dict | None = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = merged_config(current)
    database = payload.get("database") if isinstance(payload.get("database"), dict) else {}
    result = pinchuang.normalize_config(payload, current)
    # Never fall back to the single-table hub database.
    result["database"]["database"] = pinchuang._clean_text(
        database.get("database", current["database"]["database"]), 128, "数据库名称"
    ) or DEFAULT_DATABASE
    transfer = payload.get("transfer", {})
    if not isinstance(transfer, dict):
        raise ValueError("下载上传配置必须是一个对象")
    result["transfer"] = {
        key: validate_transfer_workers(transfer.get(key, current["transfer"][key]), label)
        for key, label in (("download_workers", "下载并发数"), ("upload_workers", "上传并发数"))
    }
    return result


def public_config(config: dict) -> dict:
    result = pinchuang.public_config(merged_config(config))
    result["storage_path"] = str(CONFIG_FILE)
    result.pop("table", None)
    result["tables"] = list(TABLES)
    result["transfer"] = dict(merged_config(config)["transfer"])
    return result


class CompetitorMonitorHub(CompetitorRecovery, pinchuang.PinchuangHub):
    module_name = "竞对监测"
    require_feishu = False
    config_backup_format = "competitor_monitor_config"
    thread_prefix = "competitor_monitor"
    _merge_config = staticmethod(merged_config)
    _normalize_config = staticmethod(normalize_config)
    _public_config = staticmethod(public_config)

    def __init__(self, config_path=CONFIG_FILE, state_path=STATE_FILE, *, now=pinchuang.beijing_now):
        super().__init__(config_path, state_path, now=now)

    @staticmethod
    def _load_author_videos(author):
        from backend import channels
        from backend.channels_storage import read_feeds
        feeds = read_feeds(channels.CHANNELS_FEEDS_FILE)
        videos = feeds.get(str(author.get("username") or ""))
        if videos is None:
            videos = feeds.get(str(author.get("nickname") or ""), [])
        return [dict(v) for v in videos if isinstance(v, dict)]

    def _database_adapter(self, config=None):
        return CompetitorMySQLAdapter((config if config is not None else self.config)["database"])

    def import_config_backup(self, payload):
        # Backups made before transfer settings existed remain importable.
        if isinstance(payload, dict):
            config = payload.get("config") if "config" in payload else payload
            if isinstance(config, dict) and set(config) == {"database", "schedule", "feishu"}:
                config = {**config, "transfer": dict(DEFAULT_TRANSFER)}
                payload = {**payload, "config": config} if "config" in payload else config
        return super().import_config_backup(payload)

    def _run_pipeline(self, run_id):
        with self.lock:
            run = self.state.get("current_run") or {}
            if run.get("run_id") == run_id:
                # Keep one set of limits for every creator, including recovered runs.
                run.setdefault("transfer", dict(self.config["transfer"]))
                self._persist_state_locked()
        return super()._run_pipeline(run_id)

    def _sync_new_videos_to_oss(self, run_id, author, videos):
        self._uploaded_video_urls = {}
        if not videos:
            return 0, []
        prepared, identities = [], {}
        for video in videos:
            self._pause_checkpoint(run_id)
            if self.stop_event.is_set():
                raise pinchuang._RunStopping()
            key, _ = source_key(video)
            upload_id = str(video.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", upload_id):
                upload_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
            identities[upload_id] = key
            # An upload filename is not a platform ID. Keep the original feed unchanged.
            prepared.append({**video, "id": upload_id})
        with self.lock:
            run = self.state.get("current_run") or {}
            transfer = dict(run.get("transfer", self.config["transfer"]))
        batch_id = upload_manager.start_selected_sync(
            author, prepared, transfer=transfer, reuse_remote=True,
            resume_event=self.resume_event, stop_event=self.stop_event,
            pause_checkpoint=lambda: self._pause_checkpoint(run_id),
        )["batch_id"]
        while True:
            snapshot = upload_manager.snapshot(batch_id=batch_id)
            tasks = [t for t in snapshot.get("items", []) if t.get("batch_id") == batch_id]
            if not tasks:
                raise RuntimeError("OSS 同步任务状态丢失")
            done = [t for t in tasks if t.get("status") in {"completed", "skipped"}]
            receipts = {identities[t["video_id"]]: t["oss_url"] for t in done if t.get("oss_url")}
            progress = getattr(self, "_current_creator_progress", None)
            if progress is not None:
                progress["uploaded_videos"] = len(receipts)
            context = getattr(self, "_active_monitor_checkpoint", None)
            if context and context[0] == run_id:
                checkpoint = context[2]
                if any(checkpoint.get("uploads", {}).get(k) != v for k, v in receipts.items()):
                    checkpoint.setdefault("uploads", {}).update(receipts)
                    checkpoint["progress"] = copy.deepcopy(progress or {})
                    self.journal.save_checkpoint(context[0], context[1], checkpoint)
            with self.lock:
                # Do not overwrite a pause acknowledgement from the OSS coordinator.
                if self.resume_event.is_set() and (self.state.get("current_run") or {}).get("status") != "paused":
                    downloading = sum(t.get("status") == "downloading" for t in tasks)
                    uploading = sum(t.get("status") == "uploading" for t in tasks)
                    self._update_run(run_id, phase="uploading_oss", message=(
                        f"正在同步 OSS：{len(done)}/{len(tasks)}"
                        f"（下载 {downloading}/{transfer['download_workers']}，上传 {uploading}/{transfer['upload_workers']}）"
                    ))
            running = snapshot.get("running") and snapshot.get("batch_id") == batch_id
            if not running and not any(t.get("status") in {"pending", "downloading", "uploading"} for t in tasks):
                if self.stop_event.is_set():
                    raise pinchuang._RunStopping()
                self._uploaded_video_urls = receipts
                failures = [{**t, "video_id": identities.get(t.get("video_id"), t.get("video_id"))}
                            for t in tasks if t.get("status") == "failed"]
                failures.extend({"video_id": identities[t["video_id"]], "stage": "upload_verify",
                                 "error": "传输任务已结束，但未返回可用的 OSS 地址"} for t in done if not t.get("oss_url"))
                return len(receipts), failures
            time.sleep(1)

    def _run_creator(self, run_id, sync_batch_id, author, adapter):
        if isinstance(adapter, CompetitorMySQLAdapter):
            adapter.retry_checkpoint = lambda: self._pause_checkpoint(run_id)
        author_id = str(author.get("username") or "")
        checkpoint = self.journal.checkpoint(run_id, author_id)
        with self.lock:
            run = dict(self.state.get("current_run") or {})
        selection = run.get("retry_selection", {}).get(author_id, {})
        source = self.journal.checkpoint(run["retry_source_run_id"], author_id) if run.get("retry_source_run_id") else {}
        started_at = checkpoint.get("started_at") or pinchuang.format_beijing(self.now())
        progress = self._current_creator_progress = {"started_at": started_at, "stage": "capture"}
        self._active_monitor_checkpoint = (run_id, author_id, checkpoint)
        failures = {}
        timings = {}
        captured = checkpoint.get("observations")
        if captured is None:
            refresh_started = time.time()
            self._last_refresh_task_id = None
            refreshed_count = self._refresh_author(run_id, author)
            self._pause_checkpoint(run_id)
            task_id = self._last_refresh_task_id
            fresh = [v for v in self._load_author_videos(author)
                     if (v.get("capture_task_id") == task_id if task_id else
                         isinstance(v.get("collected_at"), (int, float)) and v["collected_at"] >= refresh_started)]
            transport = _index_observations(fresh)
            fresh = list(transport.values())
            progress["refreshed_videos"] = len(fresh)
            if refreshed_count and not fresh:
                raise RuntimeError("刷新已返回，但没有本轮采集原文，请重启采集工具后重新同步")
            if refreshed_count != len(fresh):
                failures["__capture__"] = {"stage": "capture", "error_type": "CaptureCountMismatch",
                    "error": f"采集数量不一致：刷新上报 {refreshed_count} 条，实际保存 {len(fresh)} 条"}
            captured = fresh
            # A compensation run refreshes expiring download addresses, but
            # retains the original observation's metrics and capture time.
            if source.get("observations") and (selection.get("ids") or selection.get("preserve_observations")):
                old = _index_observations(source["observations"])
                captured = []
                for key in selection.get("ids", old):
                    observation = copy.deepcopy(old.get(key) or transport.get(key))
                    if observation is None:
                        continue
                    for field in ("video_url", "video_url_h264", "video_url_h265", "decode_key", "decrypt_key"):
                        if field in transport.get(key, {}):
                            observation[field] = transport[key][field]
                    if source.get("uploads", {}).get(key):
                        observation["oss_video_url"] = source["uploads"][key]
                        observation["oss_upload_status"] = "completed"
                    captured.append(observation)
            if "ids" in selection:
                wanted = set(selection["ids"])
                captured_by_key = _index_observations(captured)
                captured = [v for key, v in captured_by_key.items() if key in wanted]
                for key in wanted - captured_by_key.keys():
                    failures[key] = {"video_id": key, "stage": "capture", "error_type": "MissingRetryVideo",
                                     "error": "未找到待重试作品的采集原文，请核验作品是否仍然可见"}
            checkpoint.update(observations=copy.deepcopy(captured), started_at=started_at,
                              capture_failures=list(failures.values()), refreshed_count=refreshed_count,
                              uploads=checkpoint.get("uploads", {}))
            self.journal.save_checkpoint(run_id, author_id, checkpoint)
            timings["capture_seconds"] = round(time.time() - refresh_started, 3)
        else:
            for i, failure in enumerate(checkpoint.get("capture_failures", [])):
                failures[failure.get("video_id") or f"__capture_{i}__"] = failure
        videos = copy.deepcopy(captured)
        keyed = {}
        for video in videos:
            try:
                key, _ = source_key(video)
                keyed[key] = video
            except ValueError as exc:
                key = str(video.get("id") or "未知作品")
                failures[key] = {"video_id": key, **error_details(exc, "validation")}
        progress.update(refreshed_videos=len(videos), stage="database_read", failures=list(failures.values()))
        self._update_run(run_id, phase="checking_database", message="正在比对竞对视频主表")
        existing = adapter.latest_rows(list(keyed))
        if selection.get("missing_only"):
            keyed = {k: v for k, v in keyed.items() if not (existing.get(k) or {}).get("video_url")}
            existing = {k: v for k, v in existing.items() if k in keyed}
        progress.update(existing_videos=len(existing), new_videos=len(keyed) - len(existing), stage="transfer")
        pending = []
        for key, video in keyed.items():
            if not (existing.get(key) or {}).get("video_url"):
                if checkpoint.get("uploads", {}).get(key):
                    video["oss_video_url"] = checkpoint["uploads"][key]
                    video["oss_upload_status"] = "completed"
                pending.append(video)
        self._pause_checkpoint(run_id)
        transfer_started = time.monotonic()
        uploaded, upload_failures = self._sync_new_videos_to_oss(run_id, author, pending)
        timings["transfer_seconds"] = round(time.monotonic() - transfer_started, 3)
        progress["uploaded_videos"] = uploaded
        self._pause_checkpoint(run_id)
        for failure in upload_failures:
            key = str(failure.get("video_id") or "未知作品")
            failures[key] = {k: v for k, v in failure.items() if k in {
                "video_id", "error", "error_type", "error_code", "stage", "error_trace", "download_attempts", "upload_attempts", "attempt_errors"}}
            failures[key].setdefault("stage", "transfer")
            failures[key].setdefault("error", "OSS 同步失败")
        latest = {}
        for video in self._load_author_videos(author):
            try:
                latest[source_key(video)[0]] = video
            except ValueError:
                pass
        rows = []
        for key, video in keyed.items():
            if key in failures:
                continue
            prepared = dict(video)
            for field in ("oss_video_url", "oss_object_key", "oss_bucket", "oss_uploaded_at", "oss_upload_status"):
                if field in latest.get(key, {}):
                    prepared[field] = latest[key][field]
            if key in getattr(self, "_uploaded_video_urls", {}):
                prepared["oss_video_url"] = self._uploaded_video_urls[key]
            try:
                rows.append(build_row(author, prepared, sync_batch_id,
                                      existing_video_url=(existing.get(key) or {}).get("video_url", "")))
            except ValueError as exc:
                failures.setdefault(key, {"video_id": key, **error_details(exc, "validation")})
        progress.update(stage="database_write", failures=list(failures.values()))
        checkpoint["progress"] = copy.deepcopy(progress)
        self.journal.save_checkpoint(run_id, author_id, checkpoint)
        self._update_run(run_id, phase="writing_database", message="正在事务写入视频主表、标签和互动快照")
        self._pause_checkpoint(run_id)
        database_started = time.monotonic()
        written = adapter.write_snapshots(rows)
        timings["database_seconds"] = round(time.monotonic() - database_started, 3)
        progress["database_written"] = written
        warnings = []
        if not selection:
            previous = [c for r in self.state.get("history", []) for c in r.get("creators", [])
                        if c.get("author_id") == author_id and c.get("status") == "completed"]
            if previous and previous[0].get("refreshed_videos", 0) >= 20 and len(videos) < previous[0]["refreshed_videos"] / 2:
                warnings.append(f"本轮采集 {len(videos)} 条，上次 {previous[0]['refreshed_videos']} 条；请核验下架或可见范围变化")
        return {
            "author_id": author_id, "author_name": str(author.get("nickname") or author_id),
            "status": "partial" if failures else "completed",
            "message": ("部分作品失败" if failures else "三表同步完成") + ("；" + "；".join(warnings) if warnings else ""),
            "started_at": started_at, "finished_at": pinchuang.format_beijing(self.now()),
            "refreshed_videos": len(videos), "existing_videos": len(existing),
            "new_videos": len(keyed) - len(existing), "uploaded_videos": uploaded,
            "database_written": written, "failed_items": len(failures),
            "failures": list(failures.values()), "warnings": warnings, "timings": timings,
        }


competitor_monitor_hub = CompetitorMonitorHub()


@competitor_monitor_bp.get("/config")
def get_config_endpoint():
    return jsonify(competitor_monitor_hub.get_config())


@competitor_monitor_bp.post("/config")
def save_config_endpoint():
    try:
        config = competitor_monitor_hub.save_config(request.get_json(silent=True) or {})
        return jsonify({**config, "message": "竞对监测配置已保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@competitor_monitor_bp.post("/config/export")
def export_config_endpoint():
    response = jsonify(competitor_monitor_hub.export_config_backup())
    response.headers["Cache-Control"] = "no-store"
    return response


@competitor_monitor_bp.post("/config/import")
def import_config_endpoint():
    try:
        config = competitor_monitor_hub.import_config_backup(request.get_json(silent=True))
        return jsonify({**config, "message": "竞对监测配置已导入并保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError:
        return jsonify({"error": "配置保存失败，原配置未更改，请检查文件权限或磁盘空间"}), 500


@competitor_monitor_bp.post("/test-database")
def test_database_endpoint():
    try:
        result = competitor_monitor_hub.test_database()
        return jsonify({"message": "MySQL 连接和目标表检查成功", **result})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"MySQL 连接失败：{exc}"}), 400


@competitor_monitor_bp.post("/test-feishu")
def test_feishu_endpoint():
    try:
        result = competitor_monitor_hub.test_feishu()
        return jsonify({"message": "飞书机器人测试消息已发送", **result})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@competitor_monitor_bp.post("/runs")
def start_run_endpoint():
    try:
        run = competitor_monitor_hub.start_run(trigger="manual")
        return jsonify({"message": "竞对监测同步任务已创建", **run}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@competitor_monitor_bp.post("/runs/pause")
def pause_run_endpoint():
    try:
        run = competitor_monitor_hub.pause_run()
        return jsonify({"message": "暂停请求已提交", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@competitor_monitor_bp.post("/runs/resume")
def resume_run_endpoint():
    try:
        run = competitor_monitor_hub.resume_run()
        return jsonify({"message": "任务已继续执行", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@competitor_monitor_bp.get("/status")
def status_endpoint():
    return jsonify(competitor_monitor_hub.snapshot())


@competitor_monitor_bp.get("/runs/<run_id>/failures")
def failures_endpoint(run_id):
    try:
        response = jsonify(competitor_monitor_hub.failure_page(
            run_id, request.args.get("author_id"), int(request.args.get("offset", 0)),
            int(request.args.get("limit", 50))))
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@competitor_monitor_bp.post("/runs/<run_id>/retry-failed")
def retry_failed_endpoint(run_id):
    try:
        return jsonify(competitor_monitor_hub.retry_failed_run(run_id)), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@competitor_monitor_bp.post("/runs/<run_id>/continue")
def continue_saved_run_endpoint(run_id):
    try:
        return jsonify(competitor_monitor_hub.resume_saved_run(run_id)), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


def _author_tag_store():
    with competitor_monitor_hub.lock:
        adapter = competitor_monitor_hub._database_adapter()
    return CompetitorAuthorTagStore(adapter.connect)


@competitor_monitor_bp.get("/authors")
def authors_endpoint():
    try:
        response = jsonify({"items": _author_tag_store().list_accounts()})
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except pymysql.MySQLError:
        return jsonify({"error": "账号列表读取失败，请检查竞对监测数据库连接及账号标签表权限"}), 503


@competitor_monitor_bp.post("/author-tags/batch")
def author_tags_batch_endpoint():
    try:
        return jsonify(_author_tag_store().change_tags(request.get_json(silent=True)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (pymysql.MySQLError, RuntimeError):
        return jsonify({"error": "标签保存失败，请检查数据库连接后重试；可重复提交添加或移除操作"}), 503
