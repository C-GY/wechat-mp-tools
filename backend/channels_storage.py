"""Serialize feed mutations and publish only complete JSON files."""
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading


FEEDS_LOCK = threading.RLock()


def locked_feeds(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        with FEEDS_LOCK:
            return operation(*args, **kwargs)
    return call


def read_feeds(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("作者作品缓存格式无效，已停止写入以保留原文件")
    return data


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
