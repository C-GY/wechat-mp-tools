"""Pinchuang hub: scheduled WeChat Channels -> OSS -> MySQL synchronization.

The public interface of this module is deliberately small: configure the hub,
start a run, and inspect its durable status.  The orchestration itself stays in
the backend so scheduled work does not depend on which SPA page is open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse, urlunparse

import requests
from flask import Blueprint, jsonify, request

from backend.config import load_json
from backend.oss import get_oss_config, persistent_config_dir, upload_manager


pinchuang_bp = Blueprint("pinchuang", __name__, url_prefix="/api/pinchuang")

PLATFORM = "wechat_channels"
TARGET_TABLE = "competitor_video_snapshots"
TARGET_COLUMNS = {
    "snapshot_id",
    "platform",
    "source_video_key",
    "external_video_id",
    "author_id",
    "author_name",
    "video_title",
    "video_url",
    "published_at",
    "duration_ms",
    "like_count",
    "share_count",
    "favorite_count",
    "comment_count",
    "sync_batch_id",
    "synced_at",
    "raw_payload",
    "created_at",
}
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
PINCHUANG_CONFIG_FILE = persistent_config_dir() / "pinchuang_hub_config.json"
PINCHUANG_STATE_FILE = persistent_config_dir() / "pinchuang_hub_state.json"

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ACTIVE_RUN_STATUSES = {"queued", "running", "pausing", "paused"}
_TERMINAL_RUN_STATUSES = {"completed", "partial", "failed", "interrupted"}

DEFAULT_CONFIG = {
    "database": {
        "host": "",
        "port": 3306,
        "username": "",
        "password": "",
        "database": "pinchuang_platform",
    },
    "schedule": {
        "enabled": False,
        "times": [],
        "creator_interval_seconds": 10,
    },
    "feishu": {
        "webhook_url": "",
        "secret": "",
    },
}


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def format_beijing(value: datetime | None = None) -> str:
    value = value or beijing_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_beijing_seconds(value: datetime | None = None) -> str:
    value = value or beijing_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _merged_config(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for section in ("database", "schedule", "feishu"):
        incoming = raw.get(section)
        if isinstance(incoming, dict):
            merged[section].update(incoming)
    return merged


def normalize_schedule_times(values: Iterable[str] | None) -> list[str]:
    normalized = []
    for value in values or []:
        item = str(value or "").strip()
        if not _TIME_RE.fullmatch(item):
            raise ValueError(f"无效的触发时间：{item or '空值'}")
        if item not in normalized:
            normalized.append(item)
    return sorted(normalized)


def _clean_text(value: Any, max_length: int, label: str, required=False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符")
    return text


def normalize_config(payload: dict, current: dict | None = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = _merged_config(current)
    database = payload.get("database") if isinstance(payload.get("database"), dict) else {}
    schedule = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
    feishu = payload.get("feishu") if isinstance(payload.get("feishu"), dict) else {}

    password = str(database.get("password") or "")
    if not password and not database.get("clear_password"):
        password = str(current["database"].get("password") or "")
    secret = str(feishu.get("secret") or "")
    if not secret and not feishu.get("clear_secret"):
        secret = str(current["feishu"].get("secret") or "")

    try:
        port = int(database.get("port", current["database"].get("port", 3306)))
    except (TypeError, ValueError):
        raise ValueError("数据库端口必须是数字")
    if port < 1 or port > 65535:
        raise ValueError("数据库端口必须在 1 到 65535 之间")

    try:
        interval = int(
            schedule.get(
                "creator_interval_seconds",
                current["schedule"].get("creator_interval_seconds", 10),
            )
        )
    except (TypeError, ValueError):
        raise ValueError("创作者间隔必须是整数秒")
    if interval < 0 or interval > 86400:
        raise ValueError("创作者间隔必须在 0 到 86400 秒之间")

    webhook_url = str(feishu.get("webhook_url") or "").strip()
    if not webhook_url and not feishu.get("clear_webhook"):
        webhook_url = str(current["feishu"].get("webhook_url") or "").strip()
    webhook_url = _clean_text(webhook_url, 2048, "飞书机器人 Webhook")
    if webhook_url:
        parsed = urlparse(webhook_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("飞书机器人 Webhook 地址无效")

    times = normalize_schedule_times(
        schedule.get("times", current["schedule"].get("times", []))
    )
    schedule_enabled = bool(
        schedule.get("enabled", current["schedule"].get("enabled"))
    )
    if schedule_enabled and not times:
        raise ValueError("启用定时同步前请至少添加一个触发时间")

    return {
        "database": {
            "host": _clean_text(
                database.get("host", current["database"].get("host")), 255, "数据库地址"
            ),
            "port": port,
            "username": _clean_text(
                database.get("username", current["database"].get("username")),
                255,
                "数据库账号",
            ),
            "password": _clean_text(password, 1024, "数据库密码"),
            "database": _clean_text(
                database.get("database", current["database"].get("database")),
                128,
                "数据库名称",
            ) or "pinchuang_platform",
        },
        "schedule": {
            "enabled": schedule_enabled,
            "times": times,
            "creator_interval_seconds": interval,
        },
        "feishu": {
            "webhook_url": webhook_url,
            "secret": _clean_text(secret, 1024, "飞书机器人签名密钥"),
        },
    }


def public_config(config: dict) -> dict:
    config = _merged_config(config)
    result = json.loads(json.dumps(config))
    password = str(result["database"].pop("password", "") or "")
    secret = str(result["feishu"].pop("secret", "") or "")
    webhook_url = str(result["feishu"].pop("webhook_url", "") or "")
    result["database"]["has_password"] = bool(password)
    result["feishu"]["has_secret"] = bool(secret)
    result["feishu"]["has_webhook"] = bool(webhook_url)
    result["configured"] = bool(
        result["database"].get("host")
        and result["database"].get("username")
        and password
        and result["database"].get("database")
    )
    result["feishu"]["configured"] = bool(webhook_url)
    result["oss_configured"] = bool(get_oss_config().get("configured"))
    result["platform"] = PLATFORM
    result["table"] = TARGET_TABLE
    result["timezone"] = "Asia/Shanghai"
    result["storage_path"] = str(PINCHUANG_CONFIG_FILE)
    return result


def _normalized_source_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        parsed = parsed._replace(fragment="")
        return urlunparse(parsed)
    return text


def video_source_key(video: dict) -> tuple[str, str | None]:
    video_id = str(video.get("id") or "").strip()
    if video_id:
        return video_id[:128], video_id[:128]
    source_url = _normalized_source_url(
        video.get("detail_url")
        or video.get("share_url")
        or video.get("video_url_h264")
        or video.get("video_url")
    )
    if not source_url:
        raise ValueError("作品缺少唯一 ID 和可用 URL")
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest(), None


def _published_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, BEIJING_TZ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _unsigned_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def build_snapshot_row(
    author: dict,
    video: dict,
    sync_batch_id: str,
    *,
    existing_video_url: str = "",
    synced_at: datetime | None = None,
) -> dict:
    """Map one local feed into ``competitor_video_snapshots``."""
    source_key, external_id = video_source_key(video)
    oss_url = str(existing_video_url or video.get("oss_video_url") or "").strip()
    if not oss_url:
        raise ValueError("作品尚未生成 OSS 视频链接")
    author_id = str(author.get("username") or "").strip() or None
    author_name = str(author.get("nickname") or author_id or "").strip()
    if not author_name:
        raise ValueError("作品缺少作者名称")
    duration_seconds = _unsigned_int(video.get("duration_seconds"))
    synced = synced_at or beijing_now()
    raw_payload = {
        "platform": PLATFORM,
        "author": author,
        "video": video,
    }
    return {
        "platform": PLATFORM,
        "source_video_key": source_key,
        "external_video_id": external_id,
        "author_id": author_id,
        "author_name": author_name[:255],
        "video_title": str(video.get("description") or "").strip()[:500] or None,
        # The product contract explicitly stores the durable OSS browser URL here.
        "video_url": oss_url,
        "published_at": _published_datetime(video.get("createtime")),
        "duration_ms": duration_seconds * 1000 if duration_seconds is not None else None,
        "like_count": _unsigned_int(video.get("like_count")),
        "share_count": _unsigned_int(video.get("share_count")),
        "favorite_count": _unsigned_int(video.get("favorite_count")),
        "comment_count": _unsigned_int(video.get("comment_count")),
        "sync_batch_id": str(sync_batch_id)[:64],
        "synced_at": synced.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if synced.tzinfo
        else synced,
        "raw_payload": json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")),
    }


class MySQLSnapshotAdapter:
    """MySQL adapter for the fixed Pinchuang snapshot-table seam."""

    def __init__(self, config: dict):
        self.config = dict(config or {})

    def connect(self):
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError("缺少 PyMySQL 依赖，请重新安装完整版本") from exc
        return pymysql.connect(
            host=self.config.get("host"),
            port=int(self.config.get("port") or 3306),
            user=self.config.get("username"),
            password=self.config.get("password"),
            database=self.config.get("database") or "pinchuang_platform",
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            autocommit=False,
            init_command="SET time_zone = '+08:00'",
        )

    def test_connection(self) -> dict:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version")
                version_row = cursor.fetchone() or {}
                cursor.execute(
                    "SELECT COLUMN_NAME AS column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    (self.config.get("database"), TARGET_TABLE),
                )
                columns = {
                    str(row.get("column_name") or "")
                    for row in cursor.fetchall() or []
                }
            version = str(version_row.get("version") or "")
            if not version.startswith("8."):
                raise RuntimeError(f"目标数据库不是 MySQL 8（当前版本：{version or '未知'}）")
            if not columns:
                raise RuntimeError(f"目标表 {TARGET_TABLE} 不存在")
            missing = sorted(TARGET_COLUMNS - columns)
            if missing:
                raise RuntimeError(
                    f"目标表 {TARGET_TABLE} 缺少字段：{', '.join(missing)}"
                )
            return {"version": version, "table": TARGET_TABLE}
        finally:
            connection.close()

    def latest_rows(self, source_keys: Iterable[str]) -> dict[str, dict]:
        keys = list(dict.fromkeys(str(key) for key in source_keys if key))
        if not keys:
            return {}
        connection = self.connect()
        latest: dict[str, dict] = {}
        try:
            with connection.cursor() as cursor:
                for offset in range(0, len(keys), 500):
                    chunk = keys[offset : offset + 500]
                    placeholders = ",".join(["%s"] * len(chunk))
                    cursor.execute(
                        "SELECT source_video_key, video_url, synced_at FROM ("
                        f"SELECT source_video_key, video_url, synced_at, "
                        "ROW_NUMBER() OVER (PARTITION BY source_video_key "
                        "ORDER BY synced_at DESC, snapshot_id DESC) AS row_num "
                        f"FROM {TARGET_TABLE} WHERE platform=%s "
                        f"AND source_video_key IN ({placeholders})"
                        ") AS ranked WHERE row_num=1",
                        [PLATFORM, *chunk],
                    )
                    for row in cursor.fetchall() or []:
                        key = str(row.get("source_video_key") or "")
                        if key and key not in latest:
                            latest[key] = dict(row)
            return latest
        finally:
            connection.close()

    def write_snapshots(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        columns = (
            "platform",
            "source_video_key",
            "external_video_id",
            "author_id",
            "author_name",
            "video_title",
            "video_url",
            "published_at",
            "duration_ms",
            "like_count",
            "share_count",
            "favorite_count",
            "comment_count",
            "sync_batch_id",
            "synced_at",
            "raw_payload",
        )
        placeholders = ",".join(["%s"] * len(columns))
        # A retry of the same batch refreshes mutable metadata but deliberately
        # keeps the first durable OSS URL already stored for that snapshot.
        update_columns = (
            "external_video_id",
            "author_id",
            "author_name",
            "video_title",
            "published_at",
            "duration_ms",
            "like_count",
            "share_count",
            "favorite_count",
            "comment_count",
            "synced_at",
            "raw_payload",
        )
        updates = ",".join(f"{name}=VALUES({name})" for name in update_columns)
        sql = (
            f"INSERT INTO {TARGET_TABLE} ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        values = [tuple(row.get(column) for column in columns) for row in rows]
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.executemany(sql, values)
            connection.commit()
            return len(rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class FeishuNotifier:
    def __init__(self, webhook_url: str, secret: str = "", session=None):
        self.webhook_url = str(webhook_url or "").strip()
        self.secret = str(secret or "").strip()
        self.session = session or requests.Session()

    def configured(self) -> bool:
        return bool(self.webhook_url)

    @staticmethod
    def _card_markdown(message: str) -> str:
        lines = []
        for line in str(message or "").splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            if "：" in stripped:
                label, value = stripped.split("：", 1)
                lines.append(f"**{label}：** {value.strip()}")
            else:
                lines.append(stripped)
        return "\n".join(lines)

    def _payload(self, title: str, message: str) -> dict:
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True,
                    "enable_forward": True,
                },
                "header": {
                    "template": "blue",
                    "title": {
                        "tag": "plain_text",
                        "content": f"【{title}】",
                    },
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": self._card_markdown(message),
                        },
                    }
                ],
            },
        }
        if self.secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{self.secret}".encode("utf-8")
            signature = hmac.new(
                string_to_sign, digestmod=hashlib.sha256
            ).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(signature).decode("utf-8")
        return payload

    def send(self, title: str, message: str, attempts: int = 3) -> dict:
        if not self.configured():
            return {"sent": False, "skipped": True, "reason": "未配置飞书机器人"}
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = self.session.post(
                    self.webhook_url,
                    json=self._payload(title, message),
                    timeout=(5, 10),
                )
                response.raise_for_status()
                body = response.json()
                code = body.get("code", body.get("StatusCode", 0))
                if code not in (0, "0", None):
                    raise RuntimeError(body.get("msg") or body.get("StatusMessage") or str(body))
                return {"sent": True, "attempts": attempt}
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    time.sleep(attempt)
        return {"sent": False, "attempts": attempts, "error": last_error}


def ensure_wechat_channels_available(activity_max_age=20.0, open_timeout=8.0) -> dict:
    """Check the injected WeChat page and open it only when it is unavailable."""
    if sys.platform != "win32":
        raise RuntimeError("品创中枢自动同步目前仅支持 Windows 微信客户端")
    from backend.mitm_proxy import ProxyManager, wait_for_channels_activity
    from backend.wechat_automation import open_wechat_video_channels

    manager = ProxyManager.get_instance()
    if not manager.running and not manager.start():
        raise RuntimeError("微信极速同步助手启动失败，请检查本地代理端口")

    recent = wait_for_channels_activity(time.time() - activity_max_age, timeout=0)
    if recent is not None:
        return {
            "proxy_running": True,
            "monitoring_active": True,
            "opened": False,
            "message": "检测到视频号页面正在运行",
        }

    started_at = time.time() - 1
    result = open_wechat_video_channels()
    activity = wait_for_channels_activity(started_at, timeout=open_timeout)
    if activity is None:
        raise RuntimeError("已尝试打开视频号，但未检测到页面联网，请检查微信客户端")
    return {
        **(result or {}),
        "proxy_running": manager.running,
        "monitoring_active": True,
        "opened": True,
        "message": "视频号已自动打开，监听已就绪",
    }


class PinchuangHub:
    def __init__(
        self,
        config_path: Path = PINCHUANG_CONFIG_FILE,
        state_path: Path = PINCHUANG_STATE_FILE,
        *,
        now: Callable[[], datetime] = beijing_now,
    ):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.now = now
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self._pause_context: dict[str, tuple[str, str]] = {}
        self.worker: threading.Thread | None = None
        self.scheduler_thread: threading.Thread | None = None
        self.config = _merged_config(load_json(self.config_path, {}))
        self.state = load_json(self.state_path, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.state.setdefault("current_run", None)
        self.state.setdefault("history", [])
        self.state.setdefault("schedule_marks", {})
        current = self.state.get("current_run")
        self.interrupted_run = None
        if isinstance(current, dict) and current.get("status") in _ACTIVE_RUN_STATUSES:
            current.update(
                {
                    "status": "interrupted",
                    "phase": "interrupted",
                    "message": "软件曾在同步过程中退出，任务已中断",
                    "finished_at": format_beijing(self.now()),
                }
            )
            self.interrupted_run = dict(current)
            self._archive_current_locked()
        self._persist_state_locked()

    def _persist_state_locked(self):
        _atomic_save_json(self.state_path, self.state)

    def _archive_current_locked(self):
        current = self.state.get("current_run")
        if not isinstance(current, dict):
            return
        history = self.state.setdefault("history", [])
        if not any(item.get("run_id") == current.get("run_id") for item in history):
            history.insert(0, json.loads(json.dumps(current, default=str)))
        self.state["history"] = history[:100]

    def get_config(self) -> dict:
        with self.lock:
            return public_config(self.config)

    def save_config(self, payload: dict) -> dict:
        with self.lock:
            updated = normalize_config(payload, self.config)
            _atomic_save_json(self.config_path, updated)
            self.config = updated
            return public_config(updated)

    def _database_adapter(self) -> MySQLSnapshotAdapter:
        return MySQLSnapshotAdapter(self.config["database"])

    def _notifier(self) -> FeishuNotifier:
        config = self.config.get("feishu", {})
        return FeishuNotifier(config.get("webhook_url"), config.get("secret"))

    def test_database(self) -> dict:
        with self.lock:
            config = json.loads(json.dumps(self.config))
        self._validate_database_config(config)
        return MySQLSnapshotAdapter(config["database"]).test_connection()

    def test_feishu(self) -> dict:
        with self.lock:
            notifier = self._notifier()
        if not notifier.configured():
            raise ValueError("请先配置飞书机器人 Webhook")
        result = notifier.send(
            "飞书机器人发送测试消息",
            "应用：【自媒体内容采集工具】\n"
            "模块：【品创中枢系统】\n"
            "内容：机器人连接测试成功\n"
            f"时间：{format_beijing_seconds(self.now())}",
        )
        if not result.get("sent"):
            raise RuntimeError(f"飞书消息发送失败（已重试 3 次）：{result.get('error', '')}")
        return result

    @staticmethod
    def _validate_database_config(config: dict):
        database = config.get("database", {})
        missing = [
            label
            for key, label in (
                ("host", "数据库地址"),
                ("username", "数据库账号"),
                ("password", "数据库密码"),
                ("database", "数据库名称"),
            )
            if not str(database.get(key) or "").strip()
        ]
        if missing:
            raise ValueError("请先配置" + "、".join(missing))

    def start(self):
        with self.lock:
            if self.scheduler_thread and self.scheduler_thread.is_alive():
                return
            self.stop_event.clear()
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                daemon=True,
                name="pinchuang-scheduler",
            )
            self.scheduler_thread.start()
            interrupted = self.interrupted_run
            self.interrupted_run = None
        if interrupted:
            threading.Thread(
                target=self._notifier().send,
                args=(
                    "品创中枢任务中断",
                    f"批次：{interrupted.get('sync_batch_id', '')}\n"
                    f"原因：软件在任务完成前退出",
                ),
                daemon=True,
                name="pinchuang-interrupted-notice",
            ).start()

    def stop(self):
        self.stop_event.set()
        self.resume_event.set()

    def _scheduler_loop(self):
        while not self.stop_event.is_set():
            try:
                self._scheduler_tick()
            except Exception:
                pass
            self.stop_event.wait(15)

    def _scheduler_tick(self):
        now = self.now().astimezone(BEIJING_TZ)
        minute = now.strftime("%H:%M")
        date_key = now.strftime("%Y-%m-%d")
        with self.lock:
            schedule = dict(self.config.get("schedule", {}))
            mark_key = f"{date_key}|{minute}"
            if (
                not schedule.get("enabled")
                or minute not in schedule.get("times", [])
                or self.state["schedule_marks"].get(mark_key)
            ):
                return
            self.state["schedule_marks"][mark_key] = format_beijing(now)
            cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")
            self.state["schedule_marks"] = {
                key: value
                for key, value in self.state["schedule_marks"].items()
                if key.split("|", 1)[0] >= cutoff
            }
            self._persist_state_locked()
        try:
            self.start_run(trigger="scheduled", scheduled_time=minute)
        except Exception as exc:
            self._record_schedule_failure(minute, str(exc))
            self._notifier().send(
                "品创中枢计划任务未运行",
                f"计划时间：{date_key} {minute}\n原因：{exc}",
            )

    def _record_schedule_failure(self, scheduled_time: str, message: str):
        now_text = format_beijing(self.now())
        run = {
            "run_id": uuid.uuid4().hex,
            "sync_batch_id": uuid.uuid4().hex,
            "trigger": "scheduled",
            "scheduled_time": scheduled_time,
            "status": "failed",
            "phase": "preflight",
            "message": message,
            "started_at": now_text,
            "finished_at": now_text,
            "total_creators": 0,
            "completed_creators": 0,
            "failed_creators": 0,
            "creators": [],
        }
        with self.lock:
            self.state["current_run"] = run
            self._archive_current_locked()
            self._persist_state_locked()

    def next_scheduled_at(self) -> str:
        with self.lock:
            schedule = dict(self.config.get("schedule", {}))
        if not schedule.get("enabled") or not schedule.get("times"):
            return ""
        now = self.now().astimezone(BEIJING_TZ)
        candidates = []
        for day_offset in (0, 1):
            day = (now + timedelta(days=day_offset)).date()
            for value in schedule["times"]:
                hour, minute = (int(part) for part in value.split(":"))
                candidate = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=BEIJING_TZ
                )
                if candidate > now:
                    candidates.append(candidate)
        return format_beijing(min(candidates)) if candidates else ""

    def snapshot(self) -> dict:
        with self.lock:
            current = json.loads(json.dumps(self.state.get("current_run"), default=str))
            history = json.loads(json.dumps(self.state.get("history", [])[:30], default=str))
            running = bool(self.worker and self.worker.is_alive())
            run_status = str((current or {}).get("status") or "")
            scheduler_running = bool(self.scheduler_thread and self.scheduler_thread.is_alive())
            schedule_enabled = bool(self.config.get("schedule", {}).get("enabled"))
        return {
            "current_run": current,
            "history": history,
            "running": running,
            "paused": running and run_status == "paused",
            "pause_requested": running and run_status == "pausing",
            "scheduler_running": scheduler_running,
            "schedule_enabled": schedule_enabled,
            "next_scheduled_at": self.next_scheduled_at(),
            "timezone": "Asia/Shanghai",
        }

    def start_run(self, *, trigger="manual", scheduled_time="") -> dict:
        with self.lock:
            self._validate_database_config(self.config)
            if not get_oss_config().get("configured"):
                raise ValueError("请先完成 OSS 配置")
            if not self._notifier().configured():
                raise ValueError("请先配置飞书机器人 Webhook")
            if self.worker and self.worker.is_alive():
                raise RuntimeError("已有品创中枢同步任务正在运行")
            self.resume_event.set()
            self._pause_context.clear()
            now_text = format_beijing(self.now())
            run_id = uuid.uuid4().hex
            run = {
                "run_id": run_id,
                "sync_batch_id": uuid.uuid4().hex,
                "trigger": trigger,
                "scheduled_time": scheduled_time,
                "status": "queued",
                "phase": "queued",
                "message": "任务已创建，正在准备执行",
                "started_at": now_text,
                "finished_at": "",
                "total_creators": 0,
                "completed_creators": 0,
                "failed_creators": 0,
                "current_creator_index": 0,
                "current_creator_id": "",
                "current_creator_name": "",
                "refreshed_videos": 0,
                "existing_videos": 0,
                "new_videos": 0,
                "uploaded_videos": 0,
                "database_written": 0,
                "failed_items": 0,
                "creators": [],
            }
            self.state["current_run"] = run
            self._persist_state_locked()
            self.worker = threading.Thread(
                target=self._run_pipeline,
                args=(run_id,),
                daemon=True,
                name=f"pinchuang-run-{run_id[:8]}",
            )
            self.worker.start()
            return dict(run)

    def pause_run(self) -> dict:
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or not self.worker or not self.worker.is_alive():
                raise RuntimeError("当前没有正在执行的品创中枢任务")
            if run.get("status") in {"pausing", "paused"}:
                return dict(run)
            self._pause_context[run["run_id"]] = (
                str(run.get("phase") or "running"),
                str(run.get("message") or ""),
            )
            self.resume_event.clear()
            run.update(
                {
                    "status": "pausing",
                    "message": "暂停请求已提交，将在当前安全步骤完成后暂停",
                }
            )
            self._persist_state_locked()
            return dict(run)

    def resume_run(self) -> dict:
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or not self.worker or not self.worker.is_alive():
                raise RuntimeError("当前没有可继续的品创中枢任务")
            if run.get("status") not in {"pausing", "paused"}:
                raise RuntimeError("当前任务没有暂停")
            was_paused = run.get("status") == "paused"
            self.resume_event.set()
            if not was_paused:
                run.update(
                    {
                        "status": "running",
                        "message": "暂停已取消，任务继续执行",
                    }
                )
                self._persist_state_locked()
            return dict(run)

    def _pause_checkpoint(self, run_id: str):
        if self.resume_event.is_set():
            return
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                return
            if run_id not in self._pause_context:
                self._pause_context[run_id] = (
                    str(run.get("phase") or "running"),
                    str(run.get("message") or ""),
                )
            run.update(
                {
                    "status": "paused",
                    "phase": "paused",
                    "message": "任务已暂停，点击“继续”后从当前进度恢复",
                }
            )
            self._persist_state_locked()

        while not self.resume_event.wait(0.25):
            if self.stop_event.is_set():
                return

        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                return
            phase, message = self._pause_context.pop(run_id, ("running", ""))
            run.update(
                {
                    "status": "running",
                    "phase": phase,
                    "message": f"已继续执行：{message}" if message else "任务已继续执行",
                }
            )
            self._persist_state_locked()

    def _wait_creator_interval(self, run_id: str, seconds: int):
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            self._pause_checkpoint(run_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.stop_event.wait(min(0.5, remaining)):
                return

    def _update_run(self, run_id: str, **changes) -> dict | None:
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                return None
            run.update(changes)
            self._persist_state_locked()
            return dict(run)

    def _creator_result(self, run_id: str, result: dict):
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                return
            run.setdefault("creators", []).append(result)
            self._persist_state_locked()

    def _finish_run(self, run_id: str, status: str, message: str):
        with self.lock:
            run = self.state.get("current_run")
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                return
            run.update(
                {
                    "status": status,
                    "phase": "finished",
                    "message": message,
                    "finished_at": format_beijing(self.now()),
                    "current_creator_id": "",
                    "current_creator_name": "",
                }
            )
            self._archive_current_locked()
            self._persist_state_locked()
            self._pause_context.pop(run_id, None)
            self.resume_event.set()

    def _refresh_author(self, run_id: str, author: dict) -> int:
        from backend.channels_refresh import get_refresh_status, start_refresh_task

        task, created = start_refresh_task([author])
        if not created:
            raise RuntimeError("另一个视频号刷新任务正在运行")
        task_id = task["task_id"]
        deadline = time.monotonic() + 30 * 60
        while time.monotonic() < deadline:
            status = get_refresh_status(task_id)
            if not status:
                raise RuntimeError("视频号刷新任务状态丢失")
            self._update_run(
                run_id,
                phase="refreshing",
                message=status.get("message") or "正在刷新创作者作品",
            )
            if status.get("status") == "completed":
                if int(status.get("failed_authors") or 0):
                    raise RuntimeError(status.get("message") or "创作者刷新失败")
                return int(status.get("total_videos") or 0)
            if status.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(status.get("message") or "创作者刷新失败")
            time.sleep(1)
        raise TimeoutError("创作者刷新超过 30 分钟")

    def _sync_new_videos_to_oss(
        self, run_id: str, author: dict, videos: list[dict]
    ) -> tuple[int, list[dict]]:
        if not videos:
            return 0, []
        result = upload_manager.start_selected_sync(author, videos)
        batch_id = result["batch_id"]
        while True:
            snapshot = upload_manager.snapshot()
            tasks = [
                item for item in snapshot.get("items", []) if item.get("batch_id") == batch_id
            ]
            active = [
                item
                for item in tasks
                if item.get("status") in {"pending", "downloading", "uploading"}
            ]
            completed = [
                item for item in tasks if item.get("status") in {"completed", "skipped"}
            ]
            self._update_run(
                run_id,
                phase="uploading_oss",
                message=f"正在同步 OSS：{len(completed)}/{len(tasks)}",
            )
            if tasks and not active:
                failures = [item for item in tasks if item.get("status") == "failed"]
                return len(completed), failures
            if not tasks:
                raise RuntimeError("OSS 同步任务状态丢失")
            time.sleep(1)

    @staticmethod
    def _load_author_videos(author: dict) -> list[dict]:
        from backend import channels

        feeds_db = load_json(channels.CHANNELS_FEEDS_FILE, {})
        username = str(author.get("username") or "")
        nickname = str(author.get("nickname") or "")
        videos = feeds_db.get(username)
        if videos is None and nickname:
            videos = feeds_db.get(nickname)
        return [dict(video) for video in videos or [] if isinstance(video, dict)]

    def _run_creator(
        self,
        run_id: str,
        sync_batch_id: str,
        author: dict,
        adapter: MySQLSnapshotAdapter,
    ) -> dict:
        started_at = format_beijing(self.now())
        refreshed_count = self._refresh_author(run_id, author)
        self._pause_checkpoint(run_id)
        videos = self._load_author_videos(author)
        keyed_videos = []
        invalid = []
        for video in videos:
            try:
                source_key, _ = video_source_key(video)
                keyed_videos.append((source_key, video))
            except Exception as exc:
                invalid.append({"video_id": str(video.get("id") or ""), "error": str(exc)})

        self._update_run(run_id, phase="checking_database", message="正在比对数据库已有作品")
        existing = adapter.latest_rows(source_key for source_key, _ in keyed_videos)
        new_videos = [video for source_key, video in keyed_videos if source_key not in existing]
        self._pause_checkpoint(run_id)
        uploaded, upload_failures = self._sync_new_videos_to_oss(run_id, author, new_videos)
        self._pause_checkpoint(run_id)

        # OSS writes its durable result back to the local feed file.
        latest_videos = self._load_author_videos(author)
        latest_by_key = {}
        for video in latest_videos:
            try:
                latest_by_key[video_source_key(video)[0]] = video
            except ValueError:
                pass

        rows = []
        failure_by_video = {}
        for failure in [*invalid, *upload_failures]:
            video_id = str(failure.get("video_id") or "未知作品")
            failure_by_video[video_id] = {
                "video_id": video_id,
                "error": str(failure.get("error") or "同步失败"),
            }
        synced_at = self.now()
        for source_key, previous_video in keyed_videos:
            video = latest_by_key.get(source_key, previous_video)
            existing_url = str((existing.get(source_key) or {}).get("video_url") or "")
            try:
                rows.append(
                    build_snapshot_row(
                        author,
                        video,
                        sync_batch_id,
                        existing_video_url=existing_url,
                        synced_at=synced_at,
                    )
                )
            except Exception as exc:
                video_id = str(video.get("id") or source_key)
                failure_by_video.setdefault(
                    video_id, {"video_id": video_id, "error": str(exc)}
                )

        self._update_run(run_id, phase="writing_database", message="正在写入 MySQL 快照表")
        written = adapter.write_snapshots(rows)
        item_failures = list(failure_by_video.values())
        return {
            "author_id": str(author.get("username") or ""),
            "author_name": str(author.get("nickname") or author.get("username") or ""),
            "status": "partial" if item_failures else "completed",
            "message": "部分作品失败" if item_failures else "同步完成",
            "started_at": started_at,
            "finished_at": format_beijing(self.now()),
            "refreshed_videos": max(refreshed_count, len(videos)),
            "existing_videos": len(existing),
            "new_videos": len(new_videos),
            "uploaded_videos": uploaded,
            "database_written": written,
            "failed_items": len(item_failures),
            "failures": item_failures[:100],
        }

    def _run_pipeline(self, run_id: str):
        notifier = self._notifier()
        try:
            with self.lock:
                config = json.loads(json.dumps(self.config))
                run = dict(self.state.get("current_run") or {})
            self._update_run(
                run_id,
                status="running",
                phase="preflight",
                message="正在检查数据库、OSS 和微信视频号环境",
            )
            self._validate_database_config(config)
            if not get_oss_config().get("configured"):
                raise ValueError("OSS 尚未配置")
            adapter = MySQLSnapshotAdapter(config["database"])
            adapter.test_connection()
            ensure_wechat_channels_available()

            from backend import channels

            favorites = load_json(channels.CHANNELS_FAVORITES_FILE, [])
            authors = []
            seen_authors = set()
            for author in favorites or []:
                if not isinstance(author, dict):
                    continue
                username = str(author.get("username") or "").strip()
                if not username or username in seen_authors:
                    continue
                seen_authors.add(username)
                authors.append(dict(author))
            if not authors:
                raise ValueError("暂无已收藏创作者")
            self._update_run(
                run_id,
                total_creators=len(authors),
                message=f"环境检查完成，共 {len(authors)} 个创作者",
            )
            self._pause_checkpoint(run_id)

            totals = {
                "completed_creators": 0,
                "failed_creators": 0,
                "refreshed_videos": 0,
                "existing_videos": 0,
                "new_videos": 0,
                "uploaded_videos": 0,
                "database_written": 0,
                "failed_items": 0,
            }
            interval = int(config.get("schedule", {}).get("creator_interval_seconds") or 0)
            for index, author in enumerate(authors, 1):
                self._pause_checkpoint(run_id)
                author_id = str(author.get("username") or "")
                author_name = str(author.get("nickname") or author_id)
                self._update_run(
                    run_id,
                    phase="checking_wechat",
                    current_creator_index=index,
                    current_creator_id=author_id,
                    current_creator_name=author_name,
                    message=f"正在检查视频号环境：{index}/{len(authors)} {author_name}",
                )
                try:
                    ensure_wechat_channels_available()
                    result = self._run_creator(
                        run_id, run["sync_batch_id"], author, adapter
                    )
                    if result["status"] == "completed":
                        totals["completed_creators"] += 1
                    else:
                        totals["failed_creators"] += 1
                        notifier.send(
                            "品创中枢创作者部分失败",
                            f"创作者：{author_name}\n批次：{run['sync_batch_id']}\n"
                            f"失败作品：{result['failed_items']} 条",
                        )
                except Exception as exc:
                    totals["failed_creators"] += 1
                    result = {
                        "author_id": author_id,
                        "author_name": author_name,
                        "status": "failed",
                        "message": str(exc),
                        "started_at": format_beijing(self.now()),
                        "finished_at": format_beijing(self.now()),
                        "refreshed_videos": 0,
                        "existing_videos": 0,
                        "new_videos": 0,
                        "uploaded_videos": 0,
                        "database_written": 0,
                        "failed_items": 1,
                        "failures": [{"error": str(exc)}],
                    }
                    notifier.send(
                        "品创中枢创作者同步失败",
                        f"创作者：{author_name}\n批次：{run['sync_batch_id']}\n原因：{exc}",
                    )
                self._creator_result(run_id, result)
                for field in (
                    "refreshed_videos",
                    "existing_videos",
                    "new_videos",
                    "uploaded_videos",
                    "database_written",
                    "failed_items",
                ):
                    totals[field] += int(result.get(field) or 0)
                self._update_run(
                    run_id,
                    **totals,
                    message=f"已处理 {index}/{len(authors)}：{author_name}",
                )
                self._pause_checkpoint(run_id)
                if index < len(authors) and interval > 0:
                    self._update_run(
                        run_id,
                        phase="creator_interval",
                        message=f"等待 {interval} 秒后处理下一位创作者",
                    )
                    self._wait_creator_interval(run_id, interval)

            status = "partial" if totals["failed_creators"] else "completed"
            message = (
                f"同步完成：成功 {totals['completed_creators']} 个，"
                f"失败/部分失败 {totals['failed_creators']} 个，"
                f"写入 {totals['database_written']} 条快照"
            )
            self._finish_run(run_id, status, message)
        except Exception as exc:
            self._finish_run(run_id, "failed", f"任务执行失败：{exc}")
            notifier.send(
                "品创中枢任务异常",
                f"批次：{run.get('sync_batch_id', run_id)}\n"
                f"时间：{format_beijing(self.now())}\n原因：{exc}",
            )


pinchuang_hub = PinchuangHub()


@pinchuang_bp.route("/config", methods=["GET"])
def get_config_endpoint():
    return jsonify(pinchuang_hub.get_config())


@pinchuang_bp.route("/config", methods=["POST"])
def save_config_endpoint():
    try:
        config = pinchuang_hub.save_config(request.get_json(silent=True) or {})
        return jsonify({**config, "message": "品创中枢配置已保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@pinchuang_bp.route("/test-database", methods=["POST"])
def test_database_endpoint():
    try:
        result = pinchuang_hub.test_database()
        return jsonify({"message": "MySQL 连接和目标表检查成功", **result})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"MySQL 连接失败：{exc}"}), 400


@pinchuang_bp.route("/test-feishu", methods=["POST"])
def test_feishu_endpoint():
    try:
        result = pinchuang_hub.test_feishu()
        return jsonify({"message": "飞书机器人测试消息已发送", **result})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@pinchuang_bp.route("/runs", methods=["POST"])
def start_run_endpoint():
    try:
        run = pinchuang_hub.start_run(trigger="manual")
        return jsonify({"message": "品创中枢同步任务已创建", **run}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@pinchuang_bp.route("/runs/pause", methods=["POST"])
def pause_run_endpoint():
    try:
        run = pinchuang_hub.pause_run()
        return jsonify({"message": "暂停请求已提交", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@pinchuang_bp.route("/runs/resume", methods=["POST"])
def resume_run_endpoint():
    try:
        run = pinchuang_hub.resume_run()
        return jsonify({"message": "任务已继续执行", **run})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@pinchuang_bp.route("/status", methods=["GET"])
def status_endpoint():
    return jsonify(pinchuang_hub.snapshot())
