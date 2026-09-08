"""Observed video -> MySQL master, additive tags, and batch interaction snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend import pinchuang

PLATFORM = pinchuang.PLATFORM
VIDEO_TABLE = "competitor_videos"
TAG_TABLE = "competitor_video_tags"
SNAPSHOT_TABLE = "competitor_video_snapshots"
METRICS = ("like_count", "share_count", "favorite_count", "comment_count")
VIDEO_COLUMNS = (
    "platform", "source_video_key", "external_video_id", "author_id", "author_name",
    "video_title", "video_url", "cover_url", "published_at", "duration_ms",
    *METRICS, "last_synced_at", "raw_payload",
)
TABLES = {
    VIDEO_TABLE: ({"video_id", *VIDEO_COLUMNS, "created_at", "updated_at"},
                  "video_id", ("platform", "source_video_key")),
    TAG_TABLE: ({"video_tag_id", "video_id", "tag_name", "created_at"},
                "video_tag_id", ("video_id", "tag_name")),
    SNAPSHOT_TABLE: ({"snapshot_id", "video_id", *METRICS, "sync_batch_id", "synced_at", "created_at"},
                     "snapshot_id", ("video_id", "sync_batch_id")),
}
VARCHAR_LENGTHS = {"platform": 32, "source_video_key": 128, "external_video_id": 128,
                   "author_id": 128, "author_name": 255, "video_title": 500,
                   "video_url": 2048, "cover_url": 2048, "tag_name": 128, "sync_batch_id": 64}


def _column_type(name):
    if name in VARCHAR_LENGTHS:
        return f"varchar({VARCHAR_LENGTHS[name]})"
    if name in {"video_id", "video_tag_id", "snapshot_id", *METRICS}:
        return "bigint unsigned"
    if name == "duration_ms":
        return "int unsigned"
    if name == "raw_payload":
        return "json"
    return "datetime(3)"


def _text(value, length, label, required=False):
    return pinchuang._clean_text(value, length, label, required=required) or None


def _url(value, label, required=False):
    value = _text(value, 2048, label, required=required)
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError(f"{label}必须是完整的 HTTP(S) 地址")
    return value


def source_key(video):
    external_id = _text(video.get("id"), 128, "平台视频 ID")
    if external_id:
        return external_id, external_id
    value = next((video.get(k) for k in ("detail_url", "share_url", "video_url_h264", "video_url") if video.get(k)), None)
    parsed = urlsplit(_url(value, "视频来源链接", required=True))
    # Keep query values (including identity), but normalize order and fragments.
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                             urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), None


def _number(value, *, multiplier=1, maximum=2**64 - 1, round_fraction=False):
    if value is None or isinstance(value, bool) or str(value).strip() in {"", "--", "-"}:
        return None
    text = str(value).strip().lower().replace(",", "")
    # A lower bound such as 1万+ is not an observed total.
    if text.endswith("+"):
        return None
    units = {"万": 10000, "w": 10000, "k": 1000, "亿": 100000000}
    if text[-1:] in units:
        multiplier *= units[text[-1]]
        text = text[:-1]
    try:
        number = Decimal(text) * multiplier
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    if number > maximum:
        raise ValueError("数值超过目标数据库字段范围")
    if round_fraction:
        number = number.to_integral_value(rounding=ROUND_HALF_UP)
    if number != number.to_integral_value():
        return None
    return int(number)


def _datetime(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            stamp = Decimal(str(value))
            if not stamp.is_finite() or stamp <= 0:
                return None
            if stamp > 10_000_000_000:
                stamp /= 1000
            result = datetime.fromtimestamp(float(stamp), pinchuang.BEIJING_TZ)
        except (InvalidOperation, ValueError, OverflowError, OSError):
            try:
                result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
    if result.tzinfo:
        result = result.astimezone(pinchuang.BEIJING_TZ).replace(tzinfo=None)
    if result.year < 1000:
        return None
    return result.replace(microsecond=result.microsecond // 1000 * 1000)


def video_tags(video, raw, description):
    values = []
    for source in (video, raw, raw.get("objectDesc") or {}):
        for field in ("tags", "tag_names", "topics", "topic"):
            items = source.get(field)
            if isinstance(items, str):
                items = re.split(r"[,，;；\s]+", items)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item = item.get("tag_name") or item.get("name") or item.get("topicName")
                    if isinstance(item, str):
                        values.append(item)
    values.extend(re.findall(r"[#＃]([^\s#＃,，。！？!?;；:：、/\\\[\]（）()<>《》]+)", description))
    result = []
    for value in values:
        tag = _text(value.strip().lstrip("#＃"), 128, "视频标签")
        if tag and tag not in result:
            result.append(tag)
    return result


def build_row(author, video, sync_batch_id, *, existing_video_url="", synced_at=None):
    key, external_id = source_key(video)
    raw = video.get("rpa_payload")
    raw = raw if isinstance(raw, dict) else video
    desc = raw.get("objectDesc") or {}
    media_list = desc.get("media") or []
    media = media_list[0] if media_list and isinstance(media_list[0], dict) else {}
    contact = raw.get("contact") or {}
    author_id = _text(contact.get("username") or author.get("username"), 128, "作者 ID")
    description = str(raw.get("description") or raw.get("title") or desc.get("description") or video.get("description") or "").strip()
    observed = _datetime(video.get("collected_at")) if video.get("collected_at") is not None else _datetime(synced_at or pinchuang.beijing_now())
    if observed is None:
        raise ValueError("采集完成时间无效")
    duration_ms = _number(raw.get("duration_ms"), maximum=2**32 - 1, round_fraction=True)
    if duration_ms is None:
        for source in (raw, media):
            for field in ("duration_seconds", "videoPlayLen", "duration", "videoDuration"):
                if source.get(field) is not None:
                    duration_ms = _number(source[field], multiplier=1000, maximum=2**32 - 1, round_fraction=True)
                    break
            if duration_ms is not None:
                break
    from backend.channels import _INTERACTION_METRIC_KEYS
    metrics = {}
    for field, candidates in _INTERACTION_METRIC_KEYS.items():
        # Read this observation, never a metric inherited from the old cache.
        value = next((raw[k] for k in candidates if raw.get(k) is not None), None)
        metrics[field] = _number(value)
    return {
        "platform": PLATFORM, "source_video_key": key, "external_video_id": external_id,
        "author_id": author_id,
        "author_name": _text(contact.get("nickname") or author.get("nickname") or author_id, 255, "作者名称", True),
        "video_title": description[:500] or None,
        "video_url": _url(existing_video_url or video.get("oss_video_url"), "OSS 视频链接", True),
        "cover_url": _url(media.get("coverUrl") or raw.get("coverUrl") or raw.get("cover_url") or video.get("cover_url"), "封面链接"),
        "published_at": _datetime(raw.get("createtime", video.get("createtime"))),
        "duration_ms": duration_ms, **metrics,
        "last_synced_at": observed, "synced_at": observed,
        "sync_batch_id": _text(sync_batch_id, 64, "同步批次 ID", True),
        "tags": video_tags(video, raw, description),
        "raw_payload": json.dumps({"platform": PLATFORM, "author": author, "video": video}, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    }


class CompetitorMySQLAdapter(pinchuang.MySQLSnapshotAdapter):
    def connect(self):
        if not self.config.get("database"):
            raise ValueError("请填写竞对监测数据库名称")
        return super().connect()

    def test_connection(self):
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version")
                version = str(cursor.fetchone()["version"])
                if not version.startswith("8."):
                    raise RuntimeError(f"目标数据库不是 MySQL 8（当前版本：{version}）")
                for table, (required, primary, identity) in TABLES.items():
                    cursor.execute("SELECT ENGINE AS engine FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (self.config["database"], table))
                    metadata = cursor.fetchone() or {}
                    if str(metadata.get("engine", "")).lower() != "innodb":
                        raise RuntimeError(f"目标表 {table} 不存在或不是 InnoDB，无法保证三表事务")
                    cursor.execute(f"SHOW COLUMNS FROM {table}")
                    columns = {r["Field"]: r for r in cursor.fetchall()}
                    missing = required - columns.keys()
                    if missing:
                        raise RuntimeError(f"目标表 {table} 缺少字段：{', '.join(sorted(missing))}")
                    for name in required:
                        actual_type = re.sub(r"^(bigint|int)\(\d+\)", r"\1", columns[name]["Type"].lower())
                        if actual_type != _column_type(name):
                            raise RuntimeError(f"目标表 {table}.{name} 必须为 {_column_type(name)}，当前为 {actual_type}")
                        nullable = name in {*METRICS, "external_video_id", "author_id", "video_title", "cover_url", "published_at", "duration_ms", "raw_payload"}
                        if (columns[name]["Null"] == "YES") != nullable:
                            raise RuntimeError(f"目标表 {table}.{name} 的 NULL 约束与三表设计不一致")
                    if "auto_increment" not in columns[primary]["Extra"]:
                        raise RuntimeError(f"目标表 {table} 的 {primary} 必须是自增主键")
                    cursor.execute(f"SHOW INDEX FROM {table}")
                    indexes = {}
                    for part in cursor.fetchall():
                        if not part["Non_unique"]:
                            indexes.setdefault(part["Key_name"], []).append(part)
                    unique = []
                    for parts in indexes.values():
                        if any(part.get("Sub_part") is not None for part in parts):
                            raise RuntimeError(f"目标表 {table} 不能使用前缀唯一索引")
                        unique.append(tuple(part["Column_name"] for part in sorted(parts, key=lambda p: p["Seq_in_index"])))
                    expected = {(primary,), identity}
                    primary_parts = indexes.get("PRIMARY", [])
                    if set(unique) != expected or len(primary_parts) != 1 or primary_parts[0]["Column_name"] != primary:
                        raise RuntimeError(f"目标表 {table} 的唯一索引必须对应 {primary} 和 ({', '.join(identity)})，请核对三表设计")
                    if table != VIDEO_TABLE:
                        cursor.execute(
                            "SELECT COLUMN_NAME AS col, REFERENCED_TABLE_NAME AS target, REFERENCED_COLUMN_NAME AS target_col "
                            "FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s "
                            "AND REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL",
                            (self.config["database"], table, self.config["database"]),
                        )
                        if not any(r["col"] == "video_id" and r["target"] == VIDEO_TABLE and r["target_col"] == "video_id" for r in cursor.fetchall()):
                            raise RuntimeError(f"目标表 {table} 缺少关联 competitor_videos.video_id 的外键")
            return {"version": version, "tables": list(TABLES), "timezone": "Asia/Shanghai"}
        finally:
            connection.close()

    def latest_rows(self, source_keys):
        keys = list(dict.fromkeys(key for key in source_keys if key))
        if not keys:
            return {}
        connection = self.connect()
        result = {}
        try:
            with connection.cursor() as cursor:
                for start in range(0, len(keys), 500):
                    chunk = keys[start:start + 500]
                    cursor.execute(f"SELECT video_id, source_video_key, video_url, last_synced_at FROM {VIDEO_TABLE} "
                                   f"WHERE platform=%s AND source_video_key IN ({','.join(['%s'] * len(chunk))})", [PLATFORM, *chunk])
                    for row in cursor.fetchall():
                        result[row["source_video_key"]] = row
            return result
        finally:
            connection.close()

    def write_snapshots(self, rows):
        """Commit all three tables together. Lock each master before its children."""
        if not rows:
            return 0
        # Stable ordering also avoids deadlocks between concurrent creator runs.
        rows = sorted(rows, key=lambda r: (r["platform"], r["source_video_key"], r["synced_at"], r["sync_batch_id"]))
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                for row in rows:
                    cursor.execute(
                        f"INSERT INTO {VIDEO_TABLE} ({','.join(VIDEO_COLUMNS)}) VALUES ({','.join(['%s'] * len(VIDEO_COLUMNS))}) "
                        "ON DUPLICATE KEY UPDATE video_id=LAST_INSERT_ID(video_id)",
                        tuple(row.get(c) for c in VIDEO_COLUMNS),
                    )
                    video_id = cursor.lastrowid
                    cursor.execute(f"SELECT last_synced_at, video_url FROM {VIDEO_TABLE} WHERE video_id=%s FOR UPDATE", (video_id,))
                    current = cursor.fetchone()
                    cursor.execute(f"SELECT snapshot_id, synced_at FROM {SNAPSHOT_TABLE} WHERE video_id=%s AND sync_batch_id=%s FOR UPDATE",
                                   (video_id, row["sync_batch_id"]))
                    snapshot = cursor.fetchone()
                    if snapshot is None:
                        fields = ("video_id", *METRICS, "sync_batch_id", "synced_at")
                        cursor.execute(f"INSERT INTO {SNAPSHOT_TABLE} ({','.join(fields)}) VALUES ({','.join(['%s'] * len(fields))})",
                                       [video_id, *(row.get(c) for c in METRICS), row["sync_batch_id"], row["synced_at"]])
                    elif row["synced_at"] >= snapshot["synced_at"]:
                        cursor.execute(f"UPDATE {SNAPSHOT_TABLE} SET {','.join(c + '=%s' for c in METRICS)},synced_at=%s WHERE snapshot_id=%s",
                                       [*(row.get(c) for c in METRICS), row["synced_at"], snapshot["snapshot_id"]])
                    is_latest = row["synced_at"] >= current["last_synced_at"]
                    if is_latest and snapshot and row["synced_at"] == current["last_synced_at"]:
                        # Match the schema's (synced_at, snapshot_id) ordering on a millisecond tie.
                        cursor.execute(f"SELECT snapshot_id FROM {SNAPSHOT_TABLE} WHERE video_id=%s ORDER BY synced_at DESC,snapshot_id DESC LIMIT 1", (video_id,))
                        is_latest = cursor.fetchone()["snapshot_id"] == snapshot["snapshot_id"]
                    if is_latest:
                        updates = [c for c in VIDEO_COLUMNS if c not in {"platform", "source_video_key", "video_url"}]
                        cursor.execute(f"UPDATE {VIDEO_TABLE} SET {','.join(c + '=%s' for c in updates)}, video_url=%s WHERE video_id=%s",
                                       [*(row.get(c) for c in updates), current["video_url"] or row["video_url"], video_id])
                        for tag in row.get("tags", []):
                            cursor.execute(f"INSERT INTO {TAG_TABLE} (video_id,tag_name) VALUES (%s,%s) "
                                           "ON DUPLICATE KEY UPDATE video_tag_id=video_tag_id", (video_id, tag))
            connection.commit()
            return len({(r["platform"], r["source_video_key"], r["sync_batch_id"]) for r in rows})
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
