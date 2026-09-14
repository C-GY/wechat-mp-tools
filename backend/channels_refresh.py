"""In-memory command channel for refreshing saved WeChat Channels authors."""

import copy
import threading
import time
import uuid


_ACTIVE_STATUSES = {"waiting", "running"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_ALL_STATUSES = _ACTIVE_STATUSES | _TERMINAL_STATUSES
COMMAND_STALE_SECONDS = 75
_task_lock = threading.RLock()
_refresh_task = None


def _pagination_diagnostic(value):
    """Keep bounded paging metadata, never opaque cursors or raw responses."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('page_number', 'attempt', 'raw_count', 'video_count', 'saved_count'):
        if type(value.get(key)) is int and 0 <= value[key] <= 1000000:
            result[key] = value[key]
    for key in ('input_cursor_present', 'output_cursor_present', 'cursor_changed',
                'cursor_repeated', 'has_more', 'continue_flag'):
        if key in value and (type(value[key]) is bool or value[key] is None):
            result[key] = value[key]
    if isinstance(value.get('reason'), str) and value['reason'] in {'repeated_cursor', 'empty_page', 'missing_cursor', 'conflicting_flags',
                               'invalid_response', 'invalid_cursor', 'invalid_flags',
                               'explicit_end', 'advance', 'no_cursor'}:
        result['reason'] = value['reason']
    if isinstance(value.get('action'), str) and value['action'] in {'retry', 'fail', 'advance', 'complete'}:
        result['action'] = value['action']
    return result or None


class CaptureRefreshError(RuntimeError):
    def __init__(self, status, username):
        super().__init__(status.get('message') or '创作者刷新失败')
        result = status.get('author_results', {}).get(username, {})
        self.pagination_incomplete = (
            status.get('status') in {'failed', 'completed'}
            and result.get('pagination_complete') is False
        )
        self.captured_count = max(0, int(result.get('persisted_count', 0)))
        self.capture_diagnostic = {
            'task_id': str(status.get('task_id', ''))[:128],
            'captured_count': self.captured_count,
        }
        pagination = _pagination_diagnostic(result.get('pagination'))
        if pagination:
            self.capture_diagnostic['pagination'] = pagination


def _public_task(task):
    if task is None:
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in task.items()
        if key not in {"authors", "claimed", "persisted_ids"}
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


def start_refresh_task(authors, *, require_receipt=False):
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
            "require_receipt": require_receipt,
            "persisted_ids": {},
            "author_results": {},
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
            "capture_protocol": 1,
        }


def update_refresh_task(payload):
    """Apply progress reported by the injected WeChat page."""
    global _refresh_task
    payload = payload or {}
    task_id = str(payload.get("task_id") or "")

    with _task_lock:
        if not _refresh_task or task_id != _refresh_task["task_id"]:
            raise ValueError("刷新任务不存在或已失效")

        if _refresh_task["status"] in _TERMINAL_STATUSES:
            return _public_task(_refresh_task)
        status = payload.get("status")
        results = payload.get("author_results")
        if isinstance(results, dict):
            allowed = {a["username"] for a in _refresh_task["authors"]}
            for username, result in results.items():
                if username in allowed and isinstance(result, dict):
                    _refresh_task["author_results"][username] = {
                        "count": int(result.get("count", 0)),
                        "persisted_count": len(_refresh_task["persisted_ids"].get(username, [])),
                        "pagination_complete": result.get("pagination_complete") is True,
                        "error": str(result.get("error", "")),
                        "pagination": _pagination_diagnostic(result.get("pagination")),
                    }
        if status == "completed" and _refresh_task.get("require_receipt"):
            errors = []
            verified = 0
            for author in _refresh_task["authors"]:
                username = author["username"]
                result = _refresh_task["author_results"].get(username, {})
                count = len(_refresh_task["persisted_ids"].get(username, []))
                verified += count
                if not result.get("pagination_complete") or result.get("error"):
                    errors.append(result.get("error") or "未收到完整分页确认，请重新打开微信视频号页面后重试")
                elif result.get("count") != count:
                    errors.append(f"采集落盘数量不一致：上报 {result.get('count')}，已保存 {count}")
            if errors:
                status = "failed"
                payload = {**payload, "failed_authors": len(errors), "message": "；".join(errors)}
            payload = {**payload, "total_videos": verified}
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


def validate_capture(task_id, username):
    if not task_id:
        return
    with _task_lock:
        if (not _refresh_task or _refresh_task["task_id"] != task_id
                or _refresh_task["status"] not in _ACTIVE_STATUSES
                or username not in {a["username"] for a in _refresh_task["authors"]}):
            raise ValueError("采集结果不属于当前刷新任务或作者")


def record_capture(task_id, username, video_ids):
    if not task_id:
        return
    with _task_lock:
        validate_capture(task_id, username)
        current = set(_refresh_task["persisted_ids"].get(username, []))
        current.update(str(value) for value in video_ids)
        _refresh_task["persisted_ids"][username] = sorted(current)
        _refresh_task["updated_at"] = time.time()


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
