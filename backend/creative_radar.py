"""Creative Radar variant of the hub, using the wx_channel external upload API.

Scheduling, WeChat refresh, OSS, notifications and run history are inherited from
PinchuangHub. Only configuration and the destination-specific sync step differ.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse, urlunparse

import requests
from flask import Blueprint, jsonify, request

from backend import pinchuang
from backend.oss import get_oss_config, persistent_config_dir


creative_radar_bp = Blueprint("creative_radar", __name__, url_prefix="/api/creative-radar")
CONFIG_FILE = persistent_config_dir() / "creative_radar_config.json"
STATE_FILE = persistent_config_dir() / "creative_radar_state.json"
DEFAULT_ENDPOINT = "http://pinguan-central-platform.fandow.com/api/external/upload"
SOURCE = "external"
PLATFORM = "wechat_channel"
BATCH_SIZE = 200
DEFAULT_SYNC_TIMEOUT_SECONDS = 600


def merged_config(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    common = pinchuang._merged_config(raw)
    api = {"endpoint": DEFAULT_ENDPOINT, "api_key": "", "timeout_seconds": DEFAULT_SYNC_TIMEOUT_SECONDS}
    if isinstance(raw.get("api"), dict):
        api.update({key: raw["api"][key] for key in api if key in raw["api"]})
    return {"api": api, "schedule": common["schedule"], "feishu": common["feishu"]}


def normalize_config(payload: dict, current: dict | None = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = merged_config(current)
    # Use the existing validation and blank-secret semantics for shared settings.
    common = pinchuang.normalize_config(
        {key: payload[key] for key in ("schedule", "feishu") if key in payload}, current
    )
    api = payload.get("api") if isinstance(payload.get("api"), dict) else {}
    endpoint = pinchuang._clean_text(
        api.get("endpoint", current["api"]["endpoint"]), 2048, "API 地址", required=True
    )
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("API 地址端口无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or any(char.isspace() for char in endpoint) or port == 0):
        raise ValueError("请填写有效的 HTTP 或 HTTPS API 地址")
    api_key = str(api.get("api_key") or "").strip()
    if not api_key and not api.get("clear_api_key"):
        if endpoint != current["api"]["endpoint"] and current["api"]["api_key"]:
            raise ValueError("更换 API 地址时请填写对应的 API Key")
        api_key = str(current["api"]["api_key"] or "")
    try:
        timeout_seconds = int(str(api.get("timeout_seconds", current["api"]["timeout_seconds"])).strip())
    except ValueError:
        raise ValueError("API 同步超时必须是整数秒") from None
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("API 同步超时必须在 1 到 600 秒之间")
    return {
        "api": {"endpoint": endpoint, "api_key": pinchuang._clean_text(api_key, 2048, "API Key"),
                "timeout_seconds": timeout_seconds},
        "schedule": common["schedule"],
        "feishu": common["feishu"],
    }


def public_config(config: dict) -> dict:
    config = merged_config(config)
    common = pinchuang.public_config(config)
    return {
        "api": {"endpoint": config["api"]["endpoint"], "has_api_key": bool(config["api"]["api_key"]),
                "timeout_seconds": config["api"]["timeout_seconds"]},
        "schedule": common["schedule"],
        "feishu": common["feishu"],
        "configured": bool(config["api"]["endpoint"] and config["api"]["api_key"]),
        "oss_configured": bool(get_oss_config().get("configured")),
        "timezone": "Asia/Shanghai",
        "source": SOURCE,
        "batch_size": BATCH_SIZE,
        "sync_timeout_seconds": config["api"]["timeout_seconds"],
        "storage_path": str(CONFIG_FILE),
    }


def video_key(video: dict) -> str:
    # The API accepts the complete export ID; MySQL's 128-character limit does
    # not apply here. Preserve it so IDs with a common prefix stay distinct.
    return str(video.get("id") or "").strip() or pinchuang.video_source_key(video)[0]


def build_api_video(author: dict, video: dict) -> dict:
    key = video_key(video)
    video_url = str(video.get("oss_video_url") or "").strip()
    if not video_url:
        raise ValueError("作品尚未生成 OSS 视频链接")
    parsed = urlparse(video_url)
    if parsed.hostname and parsed.hostname.lower() == "oss.fandow.com":
        video_url = urlunparse(parsed._replace(query="", fragment=""))
    account = str(author.get("nickname") or author.get("username") or "").strip()
    if not account:
        raise ValueError("作品缺少作者名称")
    result = {
        "platform": PLATFORM,
        "aweme_id": key,
        "title": str(video.get("description") or "").strip()[:500],
        "account_name": account[:200],
        "video_url": video_url,
        "original_url": str(video.get("original_url") or video.get("detail_url") or video.get("share_url") or video_url).strip(),
        "cover_url": str(video.get("cover_url") or ""),
    }
    # Finder's raw like/fav meanings are reversed. Match wx_channel's API
    # mapping and this application's visible 点赞 / 喜欢 labels.
    for target, source in (
        ("like_count", "favorite_count"), ("favorite_count", "like_count"),
        ("share_count", "share_count"), ("comment_count", "comment_count"),
    ):
        count = pinchuang._unsigned_int(video.get(source))
        if count is not None:
            result[target] = count  # Keep explicit zeroes so decreases reach the server.
    published = pinchuang._published_datetime(video.get("createtime"))
    if published:
        result["publish_time"] = published.strftime("%Y-%m-%d %H:%M:%S")
    seconds = pinchuang._unsigned_int(video.get("duration_seconds"))
    if seconds is not None:
        result["duration"] = f"{seconds // 60:02d}:{seconds % 60:02d}"
    return result


class CreativeRadarAdapter:
    def __init__(self, config: dict, *, session=None):
        self.config = dict(config)
        self.timeout_seconds = normalize_config({"api": config})["api"]["timeout_seconds"]
        self.session = session or requests

    def _safe_error(self, value) -> str:
        text = str(value)
        key = self.config.get("api_key")
        if key:
            text = text.replace(key, "[已隐藏]")
        return text[:2000]

    def _connection_error(self, exc: requests.RequestException) -> str:
        parsed = urlparse(self.config["endpoint"])
        host = parsed.hostname or "API 服务器"
        if ":" in host:
            host = f"[{host}]"
        target = f"{host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        if isinstance(exc, requests.exceptions.ProxyError):
            return f"访问 {target} 的代理连接失败，请检查代理配置及代理服务是否可用。"
        if isinstance(exc, requests.Timeout):
            return f"请求 {target} 超时，请检查网络连接及创意雷达服务是否正常。"
        if isinstance(exc, requests.exceptions.SSLError):
            return f"无法建立与 {target} 的 HTTPS 连接，请检查服务端证书及 API 地址。"
        detail = str(exc).lower()
        if isinstance(exc, requests.ConnectionError) and any(
            marker in detail for marker in ("10061", "connection refused", "[errno 111]", "[errno 61]")
        ):
            return (
                f"无法连接 {target}：目标端口拒绝连接。"
                "请检查创意雷达服务是否已启动、端口是否已开放，或更新 API 地址。"
            )
        return f"API 连接失败：{self._safe_error(exc)}"

    def test_connection(self) -> dict:
        # The live endpoint validates API Key before the video array. Probe the
        # same POST contract with no records, never insert synthetic test data.
        self._post_upload([], probe=True)
        return {"message": "API 连接及 API Key 校验成功（未写入视频数据）"}

    def _post_upload(self, videos: list[dict], *, probe: bool = False) -> dict:
        try:
            response = self.session.post(
                self.config["endpoint"],
                json={"api_key": self.config["api_key"], "source": SOURCE, "videos": videos},
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=(5, 30) if probe else (min(10, self.timeout_seconds), self.timeout_seconds), allow_redirects=False,
            )
            try:
                try:
                    body = response.json()
                except ValueError:
                    raise RuntimeError(f"创意雷达返回了无效 JSON（HTTP {response.status_code}）") from None
                if not isinstance(body, dict):
                    raise RuntimeError("创意雷达返回了无效响应")
                # Accept only the observed, authenticated empty-array rejection;
                # unrelated validation failures or 403 responses must stay errors.
                if (probe and not videos and response.status_code == 400
                        and body.get("code") == 400
                        and str(body.get("msg") or "").strip() == "videos 数组不能为空"):
                    return body
                if (not 200 <= response.status_code < 300
                        or ("code" in body and body["code"] not in (200, "200"))
                        or body.get("success") is False or body.get("error")):
                    raise RuntimeError(
                        f"创意雷达接口返回 HTTP {response.status_code} / code {body.get('code')}："
                        f"{self._safe_error(body.get('msg') or body.get('message') or body.get('error') or '同步失败')}"
                    )
                return body
            finally:
                response.close()
        except requests.RequestException as exc:
            raise RuntimeError(self._connection_error(exc)) from None

    def upload(self, videos: list[dict]) -> list[dict]:
        body = self._post_upload(videos)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        # A successful envelope acknowledges the batch. Per-video inserted /
        # updated lists are optional, and are never needed to submit the next batch.
        errors = data.get("errors", body.get("errors")) or []
        if not isinstance(errors, list):
            raise RuntimeError(f"接口报告错误但未提供逐条结果：{self._safe_error(errors)}")
        for result in (body, data):
            if result.get("success") is False or result.get("error"):
                raise RuntimeError(f"接口报告批次失败：{self._safe_error(result.get('error') or result.get('message') or result.get('msg') or '结果待确认')}")
            for field in ("failed", "failed_count", "error_count"):
                value = result.get(field)
                if value not in (None, "", 0, "0", False):
                    try:
                        failures = int(value)
                    except (ValueError, TypeError):
                        raise RuntimeError("接口返回失败汇总，未提供可核对的逐条结果") from None
                    if failures > len(errors):
                        raise RuntimeError(f"接口报告 {failures} 条失败，但未提供完整逐条结果，整批结果待确认")
        indices = set()
        for item in errors:
            if (not isinstance(item, dict) or type(item.get("index")) is not int
                    or not 0 <= item["index"] < len(videos) or item["index"] in indices):
                raise RuntimeError(f"接口报告错误但无法定位具体作品，整批结果待确认：{self._safe_error(json.dumps(errors, ensure_ascii=False))}")
            indices.add(item["index"])
        return errors

    def sync(self, videos: list[dict], batch_id: str, *, checkpoint, progress) -> dict:
        # Always send the full prepared list. The receiver owns idempotency.
        accepted = 0
        failures = []
        batches = []
        for offset in range(0, len(videos), BATCH_SIZE):
            checkpoint()
            chunk = videos[offset:offset + BATCH_SIZE]
            batch = {
                "sync_batch_id": batch_id, "batch_index": len(batches) + 1,
                "total_batches": (len(videos) + BATCH_SIZE - 1) // BATCH_SIZE,
                "start": offset + 1, "end": offset + len(chunk), "item_count": len(chunk),
                "status": "running", "accepted_items": 0, "failed_items": 0,
                "unconfirmed_items": 0, "started_at": pinchuang.format_beijing(),
                "message": "正在等待接口响应",
            }
            batches.append(batch)
            progress(dict(batch))
            try:
                errors = self.upload(chunk)
            except RuntimeError as exc:
                # A timeout may occur after the server has written the batch.
                # Keep one batch-level issue; do not invent individual failures.
                batch.update(status="unconfirmed", unconfirmed_items=len(chunk), message=str(exc))
                failures.append({
                    "scope": "batch", "batch_index": batch["batch_index"],
                    "start": batch["start"], "end": batch["end"],
                    "item_count": len(chunk), "error": str(exc),
                })
            else:
                failures.extend({
                    "scope": "video", "item_count": 1,
                    "video_id": chunk[item["index"]]["aweme_id"],
                    "error": self._safe_error(item.get("reason") or item.get("message") or item.get("error") or "API 未提供失败原因"),
                } for item in errors)
                batch.update(
                    status="partial" if errors else "accepted", accepted_items=len(chunk) - len(errors),
                    failed_items=len(errors),
                    message=f"接口返回 {len(errors)} 条明确失败" if errors else "批次已受理，逐条写入结果以服务端为准",
                )
                accepted += batch["accepted_items"]
            batch["finished_at"] = pinchuang.format_beijing()
            progress(dict(batch))
        return {"accepted_items": accepted, "failures": failures, "batches": batches}


class CreativeRadarHub(pinchuang.PinchuangHub):
    module_name = "创意雷达"
    config_backup_format = "creative_radar_config"
    storage_label = "API 同步"
    thread_prefix = "creative-radar"
    processed_suffix = "（按批次受理统计）"
    failure_items_label = "失败/待确认作品"
    _merge_config = staticmethod(merged_config)
    _normalize_config = staticmethod(normalize_config)
    _public_config = staticmethod(public_config)

    def __init__(self, config_path=CONFIG_FILE, state_path=STATE_FILE, *, now=pinchuang.beijing_now):
        super().__init__(config_path, state_path, now=now)

    @staticmethod
    def _validate_database_config(config: dict):
        normalized = normalize_config({}, config)
        if not normalized["api"]["api_key"]:
            raise ValueError("请先配置 API Key")

    def _database_adapter(self, config: dict | None = None) -> CreativeRadarAdapter:
        return CreativeRadarAdapter((config if config is not None else self.config)["api"])

    def test_api(self) -> dict:
        return self.test_database()

    def _run_creator(self, run_id, sync_batch_id, author, adapter):
        started_at = pinchuang.format_beijing(self.now())
        self._update_run(run_id, current_api_batches=[])
        refreshed = self._refresh_author(run_id, author)
        self._pause_checkpoint(run_id)
        videos = self._load_author_videos(author)
        keyed = {}
        failures = {}
        for video in videos:
            try:
                keyed[video_key(video)] = video
            except ValueError as exc:
                key = str(video.get("id") or "未知作品")
                failures[key] = {"video_id": key, "error": str(exc)}

        self._update_run(run_id, phase="preparing_api", message="正在整理全部作品及 OSS 链接")
        existing = {key: video for key, video in keyed.items() if str(video.get("oss_video_url") or "").strip()}
        new_videos = [video for key, video in keyed.items() if key not in existing]
        self._pause_checkpoint(run_id)
        uploaded, upload_failures = self._sync_new_videos_to_oss(run_id, author, new_videos)
        self._pause_checkpoint(run_id)
        for failure in upload_failures:
            key = str(failure.get("video_id") or "未知作品")
            failures[key] = {"video_id": key, "error": str(failure.get("error") or "OSS 同步失败")}
        latest = {}
        for video in self._load_author_videos(author):
            try:
                latest[video_key(video)] = video
            except ValueError:
                pass
        payload = []
        for key, video in keyed.items():
            try:
                payload.append(build_api_video(author, latest.get(key, video)))
            except ValueError as exc:
                failures.setdefault(key, {"video_id": key, "error": str(exc)})

        self._update_run(run_id, phase="syncing_api", message=f"正在分页同步全部 {len(payload)} 条作品")
        batch_progress = []

        def track_batch(batch):
            index = batch["batch_index"] - 1
            if index == len(batch_progress):
                batch_progress.append(batch)
            else:
                batch_progress[index] = batch
            self._update_run(
                run_id, phase="syncing_api", current_api_batches=list(batch_progress),
                message=f"API 第 {batch['batch_index']}/{batch['total_batches']} 批（第 {batch['start']}-{batch['end']} 条）：{batch['message']}",
            )

        result = adapter.sync(
            payload, sync_batch_id,
            checkpoint=lambda: self._pause_checkpoint(run_id),
            progress=track_batch,
        )
        # Local validation/OSS failures have known video IDs. Remote batch issues
        # may only identify a range, so keep those records separate.
        batch_failures = []
        for item in result["failures"]:
            if item["scope"] == "video":
                failures[item["video_id"]] = item
            else:
                batch_failures.append(item)
        all_failures = [*failures.values(), *batch_failures]
        return {
            "author_id": str(author.get("username") or ""),
            "author_name": str(author.get("nickname") or author.get("username") or ""),
            "status": "partial" if all_failures else "completed",
            "message": (f"部分作品失败或结果待确认：{all_failures[0]['error']}" if all_failures
                        else f"已提交 {len(result['batches'])} 批，接口已受理" if payload else "暂无可同步作品"),
            "started_at": started_at, "finished_at": pinchuang.format_beijing(self.now()),
            "refreshed_videos": max(refreshed, len(videos)),
            "existing_videos": len(existing), "new_videos": len(new_videos),
            "uploaded_videos": uploaded,
            # Preserve the shared counter name; this counts batch acceptance,
            # not individually confirmed database writes.
            "database_written": result["accepted_items"],
            "failed_items": sum(item.get("item_count", 1) for item in all_failures),
            "failures": all_failures[:100], "api_batches": result["batches"],
        }


creative_radar_hub = CreativeRadarHub()


@creative_radar_bp.get("/config")
def get_config_endpoint():
    return jsonify(creative_radar_hub.get_config())


@creative_radar_bp.post("/config")
def save_config_endpoint():
    try:
        return jsonify({**creative_radar_hub.save_config(request.get_json(silent=True) or {}),
                        "message": "创意雷达配置已保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@creative_radar_bp.post("/config/export")
def export_config_endpoint():
    response = jsonify(creative_radar_hub.export_config_backup())
    response.headers["Cache-Control"] = "no-store"
    return response


@creative_radar_bp.post("/config/import")
def import_config_endpoint():
    try:
        config = creative_radar_hub.import_config_backup(request.get_json(silent=True))
        return jsonify({**config, "message": "创意雷达配置已导入并保存"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError:
        return jsonify({"error": "配置保存失败，原配置未更改，请检查文件权限或磁盘空间"}), 500


@creative_radar_bp.post("/test-api")
def test_api_endpoint():
    try:
        return jsonify(creative_radar_hub.test_api())
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@creative_radar_bp.post("/test-feishu")
def test_feishu_endpoint():
    try:
        return jsonify({"message": "飞书机器人测试消息已发送", **creative_radar_hub.test_feishu()})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@creative_radar_bp.post("/runs")
def start_run_endpoint():
    try:
        return jsonify({"message": "创意雷达同步任务已创建", **creative_radar_hub.start_run(trigger="manual")}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@creative_radar_bp.post("/runs/pause")
def pause_run_endpoint():
    try:
        return jsonify({"message": "暂停请求已提交", **creative_radar_hub.pause_run()})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@creative_radar_bp.post("/runs/resume")
def resume_run_endpoint():
    try:
        return jsonify({"message": "任务已继续执行", **creative_radar_hub.resume_run()})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@creative_radar_bp.get("/status")
def status_endpoint():
    return jsonify(creative_radar_hub.snapshot())
