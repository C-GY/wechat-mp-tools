"""Guangce hub with the complete Pinchuang MySQL synchronization workflow.

Configuration, backups, scheduling and run history belong to this hub; the
WeChat Channels, OSS and snapshot pipeline use the existing implementation.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from backend import pinchuang
from backend.oss import persistent_config_dir


guangce_bp = Blueprint("guangce", __name__, url_prefix="/api/guangce")
CONFIG_FILE = persistent_config_dir() / "guangce_hub_config.json"
STATE_FILE = persistent_config_dir() / "guangce_hub_state.json"
DEFAULT_DATABASE = ""


def merged_config(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    database = raw.get("database") if isinstance(raw.get("database"), dict) else {}
    return pinchuang._merged_config({
        **raw,
        "database": {"database": DEFAULT_DATABASE, **database},
    })


def normalize_config(payload: dict, current: dict | None = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = merged_config(current)
    database = payload.get("database") if isinstance(payload.get("database"), dict) else {}
    result = pinchuang.normalize_config(payload, current)
    # Guangce requires an explicit database name; do not inherit Pinchuang's fallback.
    result["database"]["database"] = pinchuang._clean_text(
        database.get("database", current["database"]["database"]), 128, "数据库名称"
    )
    return result


def public_config(config: dict) -> dict:
    result = pinchuang.public_config(merged_config(config))
    result["storage_path"] = str(CONFIG_FILE)
    return result


class GuangceHub(pinchuang.PinchuangHub):
    module_name = "广策中枢"
    config_backup_format = "guangce_config"
    thread_prefix = "guangce"
    _merge_config = staticmethod(merged_config)
    _normalize_config = staticmethod(normalize_config)
    _public_config = staticmethod(public_config)

    def __init__(self, config_path=CONFIG_FILE, state_path=STATE_FILE, *, now=pinchuang.beijing_now):
        super().__init__(config_path, state_path, now=now)


guangce_hub = GuangceHub()


@guangce_bp.get("/config")
def get_config_endpoint():
    return jsonify(guangce_hub.get_config())


@guangce_bp.post("/config")
def save_config_endpoint():
    try:
        config = guangce_hub.save_config(request.get_json(silent=True) or {})
        return jsonify({**config, "message": "广策中枢配置已保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@guangce_bp.post("/config/export")
def export_config_endpoint():
    response = jsonify(guangce_hub.export_config_backup())
    response.headers["Cache-Control"] = "no-store"
    return response


@guangce_bp.post("/config/import")
def import_config_endpoint():
    try:
        config = guangce_hub.import_config_backup(request.get_json(silent=True))
        return jsonify({**config, "message": "广策中枢配置已导入并保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError:
        return jsonify({"error": "配置保存失败，原配置未更改，请检查文件权限或磁盘空间"}), 500


@guangce_bp.post("/test-database")
def test_database_endpoint():
    try:
        result = guangce_hub.test_database()
        return jsonify({"message": "MySQL 连接和目标表检查成功", **result})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"MySQL 连接失败：{exc}"}), 400


@guangce_bp.post("/test-feishu")
def test_feishu_endpoint():
    try:
        result = guangce_hub.test_feishu()
        return jsonify({"message": "飞书机器人测试消息已发送", **result})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@guangce_bp.post("/runs")
def start_run_endpoint():
    try:
        run = guangce_hub.start_run(trigger="manual")
        return jsonify({"message": "广策中枢同步任务已创建", **run}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@guangce_bp.post("/runs/pause")
def pause_run_endpoint():
    try:
        run = guangce_hub.pause_run()
        return jsonify({"message": "暂停请求已提交", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@guangce_bp.post("/runs/resume")
def resume_run_endpoint():
    try:
        run = guangce_hub.resume_run()
        return jsonify({"message": "任务已继续执行", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@guangce_bp.get("/status")
def status_endpoint():
    return jsonify(guangce_hub.snapshot())
