"""Portable backups for saved Channels creators."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path


BACKUP_FORMAT = "wechat_channels_favorites"
FAVORITES_LOCK = threading.RLock()


def validate_favorites(favorites):
    """Validate the whole collection before any import can change local data."""
    if not isinstance(favorites, list):
        raise ValueError("配置中的收藏列表必须是数组")
    for index, favorite in enumerate(favorites, 1):
        if not isinstance(favorite, dict):
            raise ValueError(f"第 {index} 个创作者的数据格式不正确")
        username = favorite.get("username")
        if not isinstance(username, str) or not username.strip():
            raise ValueError(f"第 {index} 个创作者缺少有效的作者 ID")
        for field in ("nickname", "head_img_url", "video_url"):
            if favorite.get(field) is not None and not isinstance(favorite[field], str):
                raise ValueError(f"第 {index} 个创作者的 {field} 必须是文本")
    return favorites


def build_favorites_backup(favorites):
    validate_favorites(favorites)
    if not favorites:
        raise ValueError("暂无已收藏创作者可导出")
    return {
        "format": BACKUP_FORMAT,
        "format_version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "creator_count": len(favorites),
        "favorites": favorites,
    }


def parse_favorites_backup(payload):
    # Also accept the original channels_favorites.json for manual recovery.
    if isinstance(payload, list):
        favorites = payload
    else:
        if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT:
            raise ValueError("请选择通过“导出配置”生成的创作者收藏 JSON 文件")
        version = payload.get("format_version")
        if type(version) is not int or version != 1:
            raise ValueError("不支持此配置文件的版本，请使用兼容版本的软件导入")
        favorites = payload.get("favorites")
        validate_favorites(favorites)
        count = payload.get("creator_count")
        if type(count) is not int or count != len(favorites):
            raise ValueError("配置中的创作者数量不一致，文件可能已损坏")
    validate_favorites(favorites)
    if not favorites:
        raise ValueError("配置文件中没有可导入的创作者")
    return favorites


def merge_favorites(current, incoming):
    """Restore missing authors while keeping newer local metadata intact."""
    validate_favorites(current)
    validate_favorites(incoming)
    merged = list(current)
    seen = {favorite["username"].strip() for favorite in current}
    imported_count = 0
    for favorite in incoming:
        username = favorite["username"].strip()
        if username in seen:
            continue
        merged.append({**favorite, "username": username})
        seen.add(username)
        imported_count += 1
    return merged, imported_count, len(incoming) - imported_count


def save_favorites_atomic(filepath, data):
    """Finish writing a sibling temporary file before replacing the original."""
    filepath = Path(filepath)
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=filepath.parent,
            prefix=f".{filepath.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, filepath)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
