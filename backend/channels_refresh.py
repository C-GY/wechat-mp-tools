"""In-memory command channel for refreshing saved WeChat Channels authors."""

import copy
import threading
import time
import uuid


_ACTIVE_STATUSES = {"waiting", "running"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_ALL_STATUSES = _ACTIVE_STATUSES | _TERMINAL_STATUSES
COMMAND_STALE_SECONDS = 75
_task_lock = threading.Lock()
_refresh_task = None


def _public_task(task):
    if task is None:
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in task.items()
        if key not in {"authors", "claimed"}
    }


def _normalize_authors(authors):
    normalized = []
    seen = set()
    for author in authors or []:
        username = str((author or {}).get("username") or "").strip()
        if not username or username in seen:
            continue
        seen.add(username)
        normalized.append({
            "username": username,
            "nickname": str((author or {}).get("nickname") or username).strip(),
        })
    return normalized


def start_refresh_task(authors):
    """Create a refresh task, or return the currently active one."""
    global _refresh_task
    normalized = _normalize_authors(authors)
    if not normalized:
        raise ValueError("没有可刷新的收藏创作者")

    with _task_lock:
        if _refresh_task and _refresh_task["status"] in _ACTIVE_STATUSES:
            return _public_task(_refresh_task), False

        now = time.time()
        _refresh_task = {
            "task_id": uuid.uuid4().hex,
            "status": "waiting",
            "authors": normalized,
            "claimed": False,
            "total_authors": len(normalized),
            "completed_authors": 0,
            "failed_authors": 0,
            "total_videos": 0,
            "current_username": "",
            "current_nickname": "",
            "message": "等待微信视频号页面响应，请在微信中保持任意视频号页面打开",
            "created_at": now,
            "updated_at": now,
        }
        return _public_task(_refresh_task), True


def claim_refresh_command():
    """Claim the pending command once from an injected WeChat page."""
    global _refresh_task
    with _task_lock:
        now = time.time()
        if (
            _refresh_task
            and _refresh_task["status"] == "running"
            and now - _refresh_task["updated_at"] > COMMAND_STALE_SECONDS
        ):
            _refresh_task["status"] = "waiting"
            _refresh_task["claimed"] = False
            _refresh_task["message"] = "原微信页面已断开，等待新页面继续刷新"

        if (
            not _refresh_task
            or _refresh_task["status"] != "waiting"
            or _refresh_task["claimed"]
        ):
            return None

        _refresh_task["claimed"] = True
        _refresh_task["status"] = "running"
        _refresh_task["message"] = "微信视频号页面已连接，准备刷新收藏创作者"
        _refresh_task["updated_at"] = now
        return {
            "task_id": _refresh_task["task_id"],
            "authors": copy.deepcopy(_refresh_task["authors"]),
        }


def update_refresh_task(payload):
    """Apply progress reported by the injected WeChat page."""
    global _refresh_task
    payload = payload or {}
    task_id = str(payload.get("task_id") or "")

    with _task_lock:
        if not _refresh_task or task_id != _refresh_task["task_id"]:
            raise ValueError("刷新任务不存在或已失效")

        status = payload.get("status")
        if status is not None:
            if status not in _ALL_STATUSES:
                raise ValueError("刷新任务状态无效")
            if _refresh_task["status"] in _TERMINAL_STATUSES:
                status = _refresh_task["status"]
            _refresh_task["status"] = status

        for field in (
            "completed_authors",
            "failed_authors",
            "total_videos",
        ):
            if field in payload:
                _refresh_task[field] = max(0, int(payload[field] or 0))

        for field in ("current_username", "current_nickname", "message"):
            if field in payload:
                _refresh_task[field] = str(payload[field] or "")

        if _refresh_task["status"] == "completed" and not _refresh_task["message"]:
            _refresh_task["message"] = "收藏创作者作品刷新完成"
        _refresh_task["updated_at"] = time.time()
        return _public_task(_refresh_task)


def get_refresh_status(task_id=None):
    with _task_lock:
        if not _refresh_task:
            return None
        if task_id and task_id != _refresh_task["task_id"]:
            return None
        return _public_task(_refresh_task)


def reset_refresh_task():
    """Clear volatile task state. Primarily used by regression tests."""
    global _refresh_task
    with _task_lock:
        _refresh_task = None
