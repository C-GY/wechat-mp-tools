"""Manual account labels, independent of video collection and its transactions."""

import re


def _text(value, maximum, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    value = value.strip()
    if len(value) > maximum or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"{label}不能超过 {maximum} 个字符或包含控制字符")
    return value


def normalize_change(payload):
    if not isinstance(payload, dict) or payload.get("operation") not in ("add", "remove"):
        raise ValueError("请选择添加或移除标签")
    accounts = payload.get("accounts")
    tags = payload.get("tags")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 500:
        raise ValueError("每次请选择 1 至 500 个账号")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 20:
        raise ValueError("每次请输入 1 至 20 个标签")
    identities = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("账号格式无效")
        identities.add((_text(account.get("platform"), 32, "平台"),
                        _text(account.get("author_id"), 128, "作者 ID")))
    return payload["operation"], sorted(identities), sorted({_text(tag, 128, "标签") for tag in tags})


class CompetitorAuthorTagStore:
    def __init__(self, connect):
        self.connect = connect

    def list_accounts(self):
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    WITH ranked AS (
                        SELECT platform,author_id,author_name,last_synced_at,
                               COUNT(*) OVER (PARTITION BY platform,author_id) AS video_count,
                               ROW_NUMBER() OVER (PARTITION BY platform,author_id
                                   ORDER BY last_synced_at DESC,video_id DESC) AS rn
                        FROM competitor_videos WHERE author_id IS NOT NULL AND author_id <> ''
                    ), accounts AS (
                        SELECT platform,author_id,author_name,video_count,last_synced_at FROM ranked WHERE rn=1
                        UNION ALL
                        SELECT t.platform,t.author_id,t.author_id,0,NULL FROM competitor_author_tags t
                        WHERE NOT EXISTS (SELECT 1 FROM competitor_videos v
                            WHERE v.platform=t.platform AND v.author_id=t.author_id)
                        GROUP BY t.platform,t.author_id
                    )
                    SELECT a.*,t.tag_name FROM accounts a
                    LEFT JOIN competitor_author_tags t ON t.platform=a.platform AND t.author_id=a.author_id
                    ORDER BY a.platform,a.author_id,t.tag_name
                """)
                accounts = {}
                for row in cursor.fetchall():
                    tag = row.pop("tag_name")
                    account = accounts.setdefault((row["platform"], row["author_id"]), {**row, "tags": []})
                    if tag is not None:
                        account["tags"].append(tag)
                for account in accounts.values():
                    if account["last_synced_at"]:
                        account["last_synced_at"] = account["last_synced_at"].isoformat(timespec="milliseconds")
                return sorted(accounts.values(), key=lambda a: (a["author_name"], a["platform"], a["author_id"]))
        finally:
            connection.close()

    def change_tags(self, payload):
        operation, accounts, tags = normalize_change(payload)
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                placeholders = ",".join(["(%s,%s)"] * len(accounts))
                params = [value for account in accounts for value in account]
                cursor.execute(
                    f"SELECT platform,author_id FROM competitor_videos WHERE (platform,author_id) IN ({placeholders}) "
                    f"UNION SELECT platform,author_id FROM competitor_author_tags WHERE (platform,author_id) IN ({placeholders})",
                    params + params,
                )
                known = {(r["platform"], r["author_id"]) for r in cursor.fetchall()}
                if any(account not in known for account in accounts):
                    raise ValueError("部分账号已不存在，请刷新账号列表后重试；本次未修改任何标签")
                if operation == "add":
                    cursor.executemany(
                        "INSERT INTO competitor_author_tags (platform,author_id,tag_name) VALUES (%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE author_tag_id=author_tag_id",
                        [(platform, author_id, tag) for platform, author_id in accounts for tag in tags],
                    )
                else:
                    cursor.execute(
                        f"DELETE FROM competitor_author_tags WHERE (platform,author_id) IN ({placeholders}) "
                        f"AND tag_name IN ({','.join(['%s'] * len(tags))})", params + tags,
                    )
                changed = cursor.rowcount
            connection.commit()
            return {"operation": operation, "accounts": len(accounts), "tags": len(tags), "changed": changed}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
