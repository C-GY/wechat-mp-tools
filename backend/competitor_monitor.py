"""Competitor monitoring with independent configuration and three-table storage."""

from __future__ import annotations

import hashlib
import re
import time

from flask import Blueprint, jsonify, request
import pymysql

from backend import pinchuang
from backend.competitor_author_tags import CompetitorAuthorTagStore
from backend.competitor_monitor_store import CompetitorMySQLAdapter, TABLES, build_row, source_key
from backend.oss import persistent_config_dir, upload_manager, validate_transfer_workers


competitor_monitor_bp = Blueprint("competitor_monitor", __name__, url_prefix="/api/competitor_monitor")
CONFIG_FILE = persistent_config_dir() / "competitor_monitor_config.json"
STATE_FILE = persistent_config_dir() / "competitor_monitor_state.json"
DEFAULT_DATABASE = "competitor_monitor"
DEFAULT_TRANSFER = {"download_workers": 2, "upload_workers": 2}


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


class CompetitorMonitorHub(pinchuang.PinchuangHub):
    module_name = "竞对监测"
    require_feishu = False
    config_backup_format = "competitor_monitor_config"
    thread_prefix = "competitor_monitor"
    _merge_config = staticmethod(merged_config)
    _normalize_config = staticmethod(normalize_config)
    _public_config = staticmethod(public_config)

    def __init__(self, config_path=CONFIG_FILE, state_path=STATE_FILE, *, now=pinchuang.beijing_now):
        super().__init__(config_path, state_path, now=now)

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
            snapshot = upload_manager.snapshot()
            tasks = [t for t in snapshot.get("items", []) if t.get("batch_id") == batch_id]
            if not tasks:
                raise RuntimeError("OSS 同步任务状态丢失")
            done = [t for t in tasks if t.get("status") in {"completed", "skipped"}]
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
                self._uploaded_video_urls = {identities[t["video_id"]]: t["oss_url"] for t in done if t.get("oss_url")}
                failures = [{**t, "video_id": identities.get(t.get("video_id"), t.get("video_id"))}
                            for t in tasks if t.get("status") == "failed"]
                return len(done), failures
            time.sleep(1)

    def _run_creator(self, run_id, sync_batch_id, author, adapter):
        started_at = pinchuang.format_beijing(self.now())
        refresh_started = time.time()
        refreshed_count = self._refresh_author(run_id, author)
        self._pause_checkpoint(run_id)
        # Cache entries for deleted/unavailable videos must not become new observations.
        videos = [v for v in self._load_author_videos(author)
                  if isinstance(v.get("collected_at"), (int, float)) and v["collected_at"] >= refresh_started]
        if refreshed_count and not videos:
            raise RuntimeError("刷新已返回，但没有本轮采集原文，请重启采集工具后重新同步")
        keyed = {}
        failures = {}
        for video in videos:
            try:
                key, _ = source_key(video)
                keyed[key] = video
            except ValueError as exc:
                failures[str(video.get("id") or "未知作品")] = str(exc)
        self._update_run(run_id, phase="checking_database", message="正在比对竞对视频主表")
        existing = adapter.latest_rows(keyed)
        # Existing rows with a missing URL also need the upload repair path.
        pending = [v for key, v in keyed.items() if not (existing.get(key) or {}).get("video_url")]
        self._pause_checkpoint(run_id)
        uploaded, upload_failures = self._sync_new_videos_to_oss(run_id, author, pending)
        self._pause_checkpoint(run_id)
        for failure in upload_failures:
            failures[str(failure.get("video_id") or "未知作品")] = str(failure.get("error") or "OSS 同步失败")
        latest = {}
        for video in self._load_author_videos(author):
            try:
                latest[source_key(video)[0]] = video
            except ValueError:
                pass
        rows = []
        for key, video in keyed.items():
            # Use the observed counts/time from before uploading; only reload OSS results.
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
                failures[key] = str(exc)
        self._update_run(run_id, phase="writing_database", message="正在事务写入视频主表、标签和互动快照")
        written = adapter.write_snapshots(rows)
        return {
            "author_id": str(author.get("username") or ""),
            "author_name": str(author.get("nickname") or author.get("username") or ""),
            "status": "partial" if failures else "completed",
            "message": "部分作品失败" if failures else "三表同步完成",
            "started_at": started_at, "finished_at": pinchuang.format_beijing(self.now()),
            "refreshed_videos": len(videos), "existing_videos": len(existing),
            "new_videos": len(keyed) - len(existing), "uploaded_videos": uploaded,
            "database_written": written, "failed_items": len(failures),
            "failures": [{"video_id": key, "error": value} for key, value in failures.items()][:100],
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
