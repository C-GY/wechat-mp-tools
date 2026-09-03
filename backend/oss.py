"""OSS configuration, upload service, and WeChat Channels sync queue.

The endpoint is fixed; the bucket and access credentials are configured
independently. Configuration lives in the user's profile so reinstalling the
application does not wipe it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from flask import Blueprint, jsonify, request

from backend.config import DATA_DIR, OUTPUT_DIR, get_settings, load_json, save_json


oss_bp = Blueprint("oss", __name__, url_prefix="/api/oss")

DEFAULT_OSS_ACCESS_KEY_ID = "marketing-video-dashboard"
DEFAULT_OSS_ENDPOINT = "https://oss.fandow.com"
DEFAULT_OSS_REGION = "oss-cn-hangzhou"
# Object-key-only records from older versions used this fixed bucket.
LEGACY_OSS_BUCKET = "marketing-video-dashboard"
DEFAULT_OSS_OBJECT_PREFIX = "wechat_channel"
OSS_UPLOAD_TASKS_FILE = DATA_DIR / "oss_upload_tasks.json"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_ACCESS_KEY_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_BUCKET = re.compile(r"^[A-Za-z0-9._-]+$")


def persistent_config_dir() -> Path:
    """Return a per-user config directory independent from the installation."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "Fandow" / "SelfMediaContentCollector"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SelfMediaContentCollector"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "self-media-content-collector"


OSS_CONFIG_FILE = persistent_config_dir() / "oss_config.json"


def _read_raw_config() -> dict:
    data = load_json(OSS_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def get_oss_config(*, include_secret: bool = False) -> dict:
    raw = _read_raw_config()
    access_key_id = str(raw.get("access_key_id") or DEFAULT_OSS_ACCESS_KEY_ID).strip()
    # Preserve the upload destination for configurations written before bucket
    # became independent from the access key ID.
    bucket = str(raw.get("bucket", access_key_id) or "").strip()
    secret = str(raw.get("access_key_secret") or "").strip()
    configuration_error = ""
    try:
        _validate_access_key_id(access_key_id)
        storage_base_url = build_oss_storage_url(bucket)
    except ValueError as exc:
        # Keep old invalid configurations readable so the user can correct them.
        storage_base_url = ""
        configuration_error = str(exc)
    result = {
        "access_key_id": access_key_id,
        "has_secret": bool(secret),
        "configured": bool(storage_base_url and secret),
        "endpoint": DEFAULT_OSS_ENDPOINT,
        "bucket": bucket,
        "storage_base_url": storage_base_url,
        "configuration_error": configuration_error,
    }
    if include_secret:
        result["access_key_secret"] = secret
    return result


def _validate_credential(value: str, label: str, max_length: int) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} 不能为空")
    if len(value) > max_length or any(char in value for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} 格式不合法")
    return value


def _validate_access_key_id(value: str) -> str:
    value = _validate_credential(value, "OSS_ACCESS_KEY_ID", 256)
    if value in {".", ".."} or not _SAFE_ACCESS_KEY_ID.fullmatch(value):
        raise ValueError(
            "OSS_ACCESS_KEY_ID 只能包含 ASCII 字母、数字、点、下划线和连字符，且不能为 . 或 .."
        )
    return value


def _validate_bucket(value: str) -> str:
    value = _validate_credential(value, "OSS_BUCKET", 256)
    if value in {".", ".."} or not _SAFE_BUCKET.fullmatch(value):
        raise ValueError("OSS_BUCKET 只能包含 ASCII 字母、数字、点、下划线和连字符，且不能为 . 或 ..")
    return value


def build_oss_storage_url(bucket: str) -> str:
    return f"{DEFAULT_OSS_ENDPOINT}/{_validate_bucket(bucket)}"


def save_oss_config(access_key_id: str, access_key_secret: str = "", bucket: str | None = None) -> dict:
    access_key_id = _validate_access_key_id(access_key_id)
    current = get_oss_config(include_secret=True)
    if bucket is None:
        # Older clients omit this field. Keep an explicitly saved bucket;
        # otherwise retain their former access-key-based destination.
        bucket = _read_raw_config().get("bucket", access_key_id)
    bucket = _validate_bucket(bucket)
    secret = str(access_key_secret or "").strip()
    if not secret:
        if current["has_secret"] and current["access_key_id"] == access_key_id:
            secret = current["access_key_secret"]
        else:
            raise ValueError("请填写 OSS_ACCESS_KEY_SECRET")
    secret = _validate_credential(secret, "OSS_ACCESS_KEY_SECRET", 1024)

    OSS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OSS_CONFIG_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(
            {"access_key_id": access_key_id, "access_key_secret": secret, "bucket": bucket},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(OSS_CONFIG_FILE)
    return get_oss_config()


def clear_oss_config() -> None:
    try:
        OSS_CONFIG_FILE.unlink()
    except FileNotFoundError:
        pass


def build_oss_object_key(material_id: str, scraped_at=None) -> str:
    material_id = str(material_id or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(material_id):
        raise ValueError("视频 ID 只能包含 ASCII 字母、数字、下划线和连字符")
    date_part = ""
    if scraped_at not in (None, ""):
        try:
            timestamp = float(scraped_at)
            date_part = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            date_part = ""
    if date_part:
        return f"{DEFAULT_OSS_OBJECT_PREFIX}/{date_part}/{material_id}.mp4"
    return f"{DEFAULT_OSS_OBJECT_PREFIX}/{material_id}.mp4"


def build_oss_public_url(object_key: str, bucket: str | None = None) -> str:
    if bucket is None:
        bucket = get_oss_config()["bucket"]
    encoded_key = quote(str(object_key).strip("/"), safe="/-_.~")
    return f"{build_oss_storage_url(bucket)}/{encoded_key}"


class _ProgressFile:
    def __init__(self, file_obj, total: int, callback=None):
        self.file_obj = file_obj
        self.total = total
        self.callback = callback
        self.uploaded = 0

    def read(self, size=-1):
        chunk = self.file_obj.read(size)
        if chunk:
            self.uploaded += len(chunk)
            if self.callback:
                self.callback(self.uploaded, self.total)
        return chunk

    def __len__(self):
        return self.total


class OSSService:
    """Small dependency-free S3-compatible uploader using AWS Signature V4."""

    def __init__(self, access_key_id: str, access_key_secret: str, session=None, *, bucket: str | None = None):
        self.access_key_id = _validate_access_key_id(access_key_id)
        self.bucket = _validate_bucket(self.access_key_id if bucket is None else bucket)
        self.access_key_secret = _validate_credential(
            access_key_secret, "OSS_ACCESS_KEY_SECRET", 1024
        )
        self.session = session or requests.Session()

    @classmethod
    def from_saved_config(cls):
        config = get_oss_config(include_secret=True)
        if config.get("configuration_error"):
            raise ValueError(config["configuration_error"])
        if not config["configured"]:
            raise ValueError("请先配置 OSS_ACCESS_KEY_SECRET")
        return cls(config["access_key_id"], config["access_key_secret"], bucket=config["bucket"])

    @staticmethod
    def _sha256_hex(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def _sign_headers(self, method: str, url: str, payload_hash: str, content_type="") -> dict:
        now = datetime.utcnow()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        short_date = now.strftime("%Y%m%d")
        parsed = urlsplit(url)
        canonical_uri = quote(parsed.path or "/", safe="/-_.~")

        header_values = {
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            header_values["content-type"] = content_type
        signed_headers = ";".join(sorted(header_values))
        canonical_headers = "".join(
            f"{name}:{header_values[name]}\n" for name in sorted(header_values)
        )
        canonical_request = (
            f"{method}\n{canonical_uri}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        scope = f"{short_date}/{DEFAULT_OSS_REGION}/s3/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            f"{amz_date}\n{scope}\n{self._sha256_hex(canonical_request.encode('utf-8'))}"
        )

        def digest(key: bytes, value: str) -> bytes:
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        date_key = digest(("AWS4" + self.access_key_secret).encode("utf-8"), short_date)
        region_key = digest(date_key, DEFAULT_OSS_REGION)
        service_key = digest(region_key, "s3")
        signing_key = digest(service_key, "aws4_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _check_response(response, operation: str):
        if 200 <= response.status_code < 300:
            return
        body = str(getattr(response, "text", "") or "").strip()[:1000]
        raise RuntimeError(
            f"{operation}失败: HTTP {response.status_code}"
            + (f": {body}" if body else "")
        )

    def upload_video(self, file_path: Path, material_id: str, scraped_at=None, callback=None):
        file_path = Path(file_path)
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            raise ValueError("待上传视频不存在或为空")
        total = file_path.stat().st_size
        if total > 5 * 1024 * 1024 * 1024:
            raise ValueError("待上传视频超过 OSS 单次 PUT 的 5 GiB 上限")
        object_key = build_oss_object_key(material_id, scraped_at)
        # Keep the target and signing credentials from the same config snapshot.
        target_url = build_oss_public_url(object_key, self.bucket)

        digest = hashlib.sha256()
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        payload_hash = digest.hexdigest()
        headers = self._sign_headers("PUT", target_url, payload_hash, "video/mp4")
        headers["Content-Length"] = str(total)
        if callback:
            callback(0, total)
        with file_path.open("rb") as source:
            response = self.session.put(
                target_url,
                data=_ProgressFile(source, total, callback),
                headers=headers,
                timeout=(10, 600),
            )
        try:
            self._check_response(response, "OSS 上传")
        finally:
            close_response = getattr(response, "close", None)
            if close_response:
                close_response()

        head_headers = self._sign_headers("HEAD", target_url, hashlib.sha256(b"").hexdigest())
        head_response = self.session.head(target_url, headers=head_headers, timeout=(10, 60))
        try:
            self._check_response(head_response, "OSS 上传后校验")
            remote_size = int(head_response.headers.get("Content-Length", -1))
            if remote_size != total:
                raise RuntimeError(
                    f"OSS 上传后大小校验失败：本地 {total} bytes，远端 {remote_size} bytes"
                )
            etag = str(head_response.headers.get("ETag", "")).strip('"')
        finally:
            close_response = getattr(head_response, "close", None)
            if close_response:
                close_response()

        verify = self.session.get(
            target_url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=(10, 15),
        )
        try:
            if verify.status_code not in (200, 206):
                self._check_response(verify, "OSS 访问链接验证")
        finally:
            close_response = getattr(verify, "close", None)
            if close_response:
                close_response()
        return {
            "object_key": object_key,
            "bucket": self.bucket,
            "url": target_url,
            "size": total,
            "etag": etag,
        }


class OSSUploadManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = []
        self.active_batch_id = ""
        self.worker = None
        self._load()

    def _load(self):
        stored = load_json(OSS_UPLOAD_TASKS_FILE, [])
        self.tasks = stored if isinstance(stored, list) else []
        changed = False
        for task in self.tasks:
            if task.get("status") in {"pending", "downloading", "uploading"}:
                task["status"] = "failed"
                task["error"] = "软件已重启，请重新发起同步"
                changed = True
        if changed:
            self._persist()

    def _persist(self):
        save_json(OSS_UPLOAD_TASKS_FILE, self.tasks[-100000:])

    def _update(self, task_id: str, **changes):
        with self.lock:
            for task in self.tasks:
                if task.get("id") == task_id:
                    task.update(changes)
                    task["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._persist()
                    return task.copy()
        return None

    def snapshot(self):
        with self.lock:
            # Keep live work visible first, then show newer published works first.
            # The original list is retained for persistence; only the API view is sorted.
            status_order = {"uploading": 0, "downloading": 1, "pending": 2}

            def sort_key(entry):
                index, task = entry
                try:
                    published_at = float(task.get("published_at") or 0)
                except (TypeError, ValueError):
                    published_at = 0
                return (
                    status_order.get(task.get("status"), 3),
                    -published_at,
                    -index,
                )

            ordered_tasks = sorted(enumerate(self.tasks), key=sort_key)
            items = [dict(task) for _, task in ordered_tasks]
            statuses = [task.get("status") for task in self.tasks]
            return {
                "items": items,
                "stats": {
                    "total": len(items),
                    "pending": statuses.count("pending"),
                    "downloading": statuses.count("downloading"),
                    "uploading": statuses.count("uploading"),
                    "completed": statuses.count("completed") + statuses.count("skipped"),
                    "failed": statuses.count("failed"),
                },
                "running": bool(self.worker and self.worker.is_alive()),
                "batch_id": self.active_batch_id,
            }

    def clear_finished(self):
        with self.lock:
            self.tasks = [
                task
                for task in self.tasks
                if task.get("status") in {"pending", "downloading", "uploading"}
            ]
            self._persist()

    def start_favorites_sync(self):
        from backend import channels

        favorites = load_json(channels.CHANNELS_FAVORITES_FILE, [])
        feeds_db = load_json(channels.CHANNELS_FEEDS_FILE, {})
        if not isinstance(favorites, list) or not favorites:
            raise ValueError("暂无已收藏创作者")

        candidates, batch_id = self._build_candidates(favorites, feeds_db)
        if not candidates:
            raise ValueError("收藏创作者暂无可同步作品")
        return self._start_candidates(batch_id, candidates)

    def start_author_sync(self, username: str):
        """Start an OSS sync containing only the requested author's works."""
        from backend import channels

        username = str(username or "").strip()
        if not username or len(username) > 512:
            raise ValueError("作者 ID 不能为空或过长")

        favorites = load_json(channels.CHANNELS_FAVORITES_FILE, [])
        feeds_db = load_json(channels.CHANNELS_FEEDS_FILE, {})
        if not isinstance(feeds_db, dict):
            feeds_db = {}
        favorite = next(
            (
                item
                for item in favorites
                if isinstance(item, dict)
                and str(item.get("username") or "").strip() == username
            ),
            None,
        )
        author = dict(favorite or {})
        author["username"] = username
        author.setdefault("nickname", username)

        candidates, batch_id = self._build_candidates([author], feeds_db)
        if not candidates:
            raise ValueError("当前创作者暂无可同步作品")
        return self._start_candidates(batch_id, candidates)

    def start_selected_sync(self, author: dict, videos: list[dict]):
        """Sync only selected works for one author.

        The Pinchuang pipeline uses this interface after its database diff so
        an existing database record never causes an unnecessary OSS upload.
        """
        author = dict(author or {})
        username = str(author.get("username") or "").strip()
        if not username:
            raise ValueError("作者 ID 不能为空")
        selected = [dict(video) for video in videos or [] if isinstance(video, dict)]
        if not selected:
            raise ValueError("当前创作者没有需要同步的新增作品")
        candidates, batch_id = self._build_candidates(
            [author], {username: selected}
        )
        if not candidates:
            raise ValueError("新增作品缺少可同步的视频 ID")
        return self._start_candidates(batch_id, candidates)

    @staticmethod
    def _build_candidates(authors, feeds_db):
        candidates = []
        batch_id = uuid.uuid4().hex
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")

        for author in authors:
            if not isinstance(author, dict):
                continue
            username = str(author.get("username") or "").strip()
            nickname = str(author.get("nickname") or username).strip()
            videos = feeds_db.get(username)
            feed_key = username
            if videos is None and nickname:
                videos = feeds_db.get(nickname)
                feed_key = nickname
            if not isinstance(videos, list):
                continue
            for video in videos:
                if not isinstance(video, dict):
                    continue
                video_id = str(video.get("id") or "").strip()
                if not video_id or not _SAFE_IDENTIFIER.fullmatch(video_id):
                    continue
                try:
                    published_at = float(video.get("createtime") or 0)
                except (TypeError, ValueError):
                    published_at = 0
                task = {
                    "id": uuid.uuid4().hex,
                    "batch_id": batch_id,
                    "video_id": video_id,
                    "username": username,
                    "feed_key": feed_key,
                    "author": nickname,
                    "title": str(video.get("description") or video_id).strip(),
                    "published_at": published_at,
                    "status": "pending",
                    "progress": 0,
                    "uploaded_bytes": 0,
                    "total_bytes": 0,
                    "oss_url": "",
                    "object_key": "",
                    "error": "",
                    "created_at": created_at,
                    "updated_at": created_at,
                }
                candidates.append((task, dict(video)))
        candidates.sort(key=lambda item: item[0]["published_at"], reverse=True)
        return candidates, batch_id

    def _start_candidates(self, batch_id, candidates):
        if not get_oss_config()["configured"]:
            raise ValueError("请先完成 OSS 配置")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("OSS 同步任务正在进行中")
            self.tasks.extend(task for task, _ in candidates)
            self.tasks = self.tasks[-100000:]
            self.active_batch_id = batch_id
            self._persist()
            self.worker = threading.Thread(
                target=self._run_batch,
                args=(batch_id, candidates),
                daemon=True,
                name=f"oss-sync-{batch_id[:8]}",
            )
            self.worker.start()
        return {"batch_id": batch_id, "total": len(candidates)}

    def _run_batch(self, batch_id, candidates):
        try:
            service = OSSService.from_saved_config()
        except Exception as exc:
            for task, _ in candidates:
                self._update(task["id"], status="failed", error=str(exc))
            return

        for task, video in candidates:
            task_id = task["id"]
            existing_url = str(video.get("oss_video_url") or "").strip()
            if existing_url and video.get("oss_upload_status") in (None, "", "completed"):
                self._update(
                    task_id,
                    status="skipped",
                    progress=100,
                    oss_url=existing_url,
                    object_key=str(video.get("oss_object_key") or ""),
                )
                continue
            temp_path = None
            try:
                temp_path = self._download_video(task_id, video)
                last_saved_percent = -1

                def on_progress(uploaded, total):
                    nonlocal last_saved_percent
                    percent = int(uploaded / total * 100) if total else 0
                    if percent == last_saved_percent and uploaded != total:
                        return
                    last_saved_percent = percent
                    self._update(
                        task_id,
                        status="uploading",
                        progress=min(100, max(0, percent)),
                        uploaded_bytes=uploaded,
                        total_bytes=total,
                    )

                result = service.upload_video(
                    temp_path,
                    task["video_id"],
                    video.get("createtime"),
                    on_progress,
                )
                self._save_video_result(task, result)
                self._update(
                    task_id,
                    status="completed",
                    progress=100,
                    uploaded_bytes=result["size"],
                    total_bytes=result["size"],
                    oss_url=result["url"],
                    object_key=result["object_key"],
                    error="",
                )
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            except Exception as exc:
                self._update(task_id, status="failed", error=str(exc))

    def _download_video(self, task_id: str, video: dict) -> Path:
        from backend.channels import decrypt_channels_data

        source_url = str(
            video.get("video_url_h264") or video.get("video_url") or ""
        ).strip()
        if not source_url:
            raise ValueError("作品缺少可用的视频下载链接，请先刷新创作者数据")
        settings = get_settings()
        base_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
        cache_dir = base_dir / "channels" / "oss_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_path = cache_dir / f"{video.get('id')}.mp4"
        if file_path.is_file() and file_path.stat().st_size > 0:
            self._update(
                task_id,
                status="downloading",
                progress=0,
                total_bytes=file_path.stat().st_size,
            )
            return file_path

        self._update(task_id, status="downloading", progress=0, error="")
        partial = file_path.with_suffix(".part")
        response = requests.get(
            source_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
            stream=True,
            timeout=(10, 120),
        )
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0) or 0)
        downloaded = 0
        last_download_percent = -1
        try:
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    progress = int(downloaded / total * 100) if total else 0
                    if progress != last_download_percent or (total and downloaded >= total):
                        last_download_percent = progress
                        self._update(
                            task_id,
                            status="downloading",
                            progress=min(99, progress),
                            uploaded_bytes=downloaded,
                            total_bytes=total,
                        )
            partial.replace(file_path)
        except Exception:
            try:
                partial.unlink()
            except OSError:
                pass
            raise
        finally:
            response.close()

        decrypt_key = video.get("decode_key") or video.get("decrypt_key")
        if decrypt_key:
            key_value = int(decrypt_key)
            if key_value > 0:
                with file_path.open("r+b") as output:
                    data = bytearray(output.read(131072))
                    decrypt_channels_data(data, key_value)
                    output.seek(0)
                    output.write(data)
        return file_path

    @staticmethod
    def _save_video_result(task: dict, result: dict):
        from backend import channels

        feeds_db = load_json(channels.CHANNELS_FEEDS_FILE, {})
        feed_key = task.get("feed_key") or task.get("username")
        videos = feeds_db.get(feed_key)
        if not isinstance(videos, list):
            return
        for video in videos:
            if isinstance(video, dict) and str(video.get("id")) == task["video_id"]:
                video["oss_video_url"] = result["url"]
                video["oss_object_key"] = result["object_key"]
                if result.get("bucket"):
                    video["oss_bucket"] = result["bucket"]
                video["oss_upload_status"] = "completed"
                video["oss_uploaded_at"] = int(time.time())
                break
        save_json(channels.CHANNELS_FEEDS_FILE, feeds_db)


upload_manager = OSSUploadManager()


@oss_bp.route("/config", methods=["GET"])
def read_config_endpoint():
    return jsonify(get_oss_config())


@oss_bp.route("/config", methods=["POST"])
def save_config_endpoint():
    data = request.get_json(silent=True) or {}
    try:
        config = save_oss_config(
            data.get("access_key_id") or data.get("accessKeyId"),
            data.get("access_key_secret") or data.get("accessKeySecret") or "",
            bucket=data.get("bucket"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**config, "message": "OSS 配置已保存"})


@oss_bp.route("/config", methods=["DELETE"])
def clear_config_endpoint():
    clear_oss_config()
    return jsonify({"message": "OSS 配置已清除", **get_oss_config()})


@oss_bp.route("/sync-favorites", methods=["POST"])
def sync_favorites_endpoint():
    try:
        result = upload_manager.start_favorites_sync()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"message": "OSS 同步任务已创建", **result}), 202


@oss_bp.route("/sync-author", methods=["POST"])
def sync_author_endpoint():
    data = request.get_json(silent=True) or {}
    try:
        result = upload_manager.start_author_sync(data.get("username"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"message": "当前创作者 OSS 同步任务已创建", **result}), 202


@oss_bp.route("/uploads", methods=["GET"])
def uploads_endpoint():
    return jsonify(upload_manager.snapshot())


@oss_bp.route("/uploads/finished", methods=["DELETE"])
def clear_uploads_endpoint():
    upload_manager.clear_finished()
    return jsonify({"message": "已清除完成和失败记录", **upload_manager.snapshot()})
