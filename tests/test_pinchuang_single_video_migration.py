import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from scripts import migrate_pinchuang_single_video as migration


class MigrationSafetyTests(unittest.TestCase):
    def test_identifiers_reject_injection_and_mysql_length_overflow(self):
        for name in ("a`b", "table;DROP TABLE x", "a" * 65, ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                migration.quoted(name)
        self.assertEqual(migration.quoted("safe_table_123"), "`safe_table_123`")

    def test_latest_row_selection_is_deterministic_and_platform_scoped(self):
        sql = migration.latest_select(["snapshot_id", "source_video_key"])
        self.assertIn("PARTITION BY platform, source_video_key", sql)
        self.assertIn("ORDER BY synced_at DESC, snapshot_id DESC", sql)
        self.assertIn("WHERE migration_rank=1", sql)

    def test_prefix_and_batch_indexes_do_not_enforce_one_row_per_video(self):
        identity = [{"Column_name": name, "Sub_part": None} for name in migration.IDENTITY]
        self.assertTrue(migration.matches_index(identity, migration.IDENTITY))
        self.assertFalse(migration.matches_index(identity + [{"Column_name": "sync_batch_id"}], migration.IDENTITY))
        identity[0]["Sub_part"] = 12
        self.assertFalse(migration.matches_index(identity, migration.IDENTITY))


@unittest.skipUnless(os.environ.get("PINCHUANG_MYSQL_TEST_CONFIG"), "Explicit MySQL integration config required")
class MySQLMigrationIntegrationTests(unittest.TestCase):
    """Uses only uniquely named synthetic tables; never mutates the live table."""

    def test_migration_backup_dedup_idempotence_and_cross_batch_upsert(self):
        from backend import pinchuang

        config_path = Path(os.environ["PINCHUANG_MYSQL_TEST_CONFIG"])
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))["database"]
        adapter = pinchuang.MySQLSnapshotAdapter(config)
        connection = adapter.connect()
        fixture = "pc_mig_" + uuid4().hex[:8]
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE TABLE {migration.quoted(fixture)} LIKE {migration.quoted(migration.TARGET)}")
                with patch.object(migration, "TARGET", fixture):
                    for name, parts in migration.unique_indexes(cursor).items():
                        if migration.matches_index(parts, migration.IDENTITY):
                            cursor.execute(f"ALTER TABLE {migration.quoted(fixture)} DROP INDEX {migration.quoted(name)}")
                    indexes = migration.unique_indexes(cursor)
                    if not any(migration.matches_index(parts, migration.LEGACY_IDENTITY) for parts in indexes.values()):
                        cursor.execute(f"ALTER TABLE {migration.quoted(fixture)} ADD UNIQUE KEY uq_test_batch (platform, source_video_key, sync_batch_id)")
                cursor.executemany(
                    f"INSERT INTO {migration.quoted(fixture)} "
                    "(snapshot_id,platform,source_video_key,author_name,video_title,video_url,"
                    "sync_batch_id,synced_at,like_count,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (i, platform, key, "测试作者", title, f"https://example.invalid/{key}.mp4", batch, synced, i, "2026-09-01 00:00:00")
                        for i, platform, key, title, batch, synced in (
                            (1, "wechat_channels", "v1", "旧标题", "b1", "2026-09-01 01:00:00"),
                            (2, "wechat_channels", "v1", "新标题", "b2", "2026-09-02 01:00:00"),
                            (3, "wechat_channels", "v2", "同时间旧记录", "b1", "2026-09-02 01:00:00"),
                            (4, "wechat_channels", "v2", "同时间新记录", "b2", "2026-09-02 01:00:00"),
                            (5, "other_platform", "v1", "其他平台", "b1", "2026-09-01 01:00:00"),
                        )
                    ],
                )
            connection.commit()
            with tempfile.TemporaryDirectory() as directory, patch.object(migration, "TARGET", fixture):
                with patch.object(migration, "write_backup", side_effect=OSError("simulated backup disk failure")):
                    with self.assertRaisesRegex(OSError, "backup disk failure"):
                        migration.migrate(connection, config["database"], Path(directory))
                with connection.cursor() as cursor:
                    unchanged = migration.inspect(cursor, config["database"])
                self.assertEqual(unchanged["row_count"], 5)
                self.assertFalse(unchanged["already_unique"])
                result = migration.migrate(connection, config["database"], Path(directory))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["original_rows"], 5)
                self.assertEqual(result["active_rows"], 3)
                self.assertTrue(Path(result["sql_backup"]).is_file())
                # Prove the local SQL backup restores every field into its own table.
                # The generated statements are one per line for these synthetic rows.
                with connection.cursor() as cursor:
                    for statement in Path(result["sql_backup"]).read_text(encoding="utf-8").split(";\n"):
                        if statement.strip():
                            cursor.execute(statement)
                    cursor.execute(f"SELECT * FROM {migration.quoted(result['backup_table'])} ORDER BY snapshot_id")
                    backup_rows = cursor.fetchall()
                    cursor.execute(f"SELECT * FROM {migration.quoted(result['recovery_table'])} ORDER BY snapshot_id")
                    self.assertEqual(cursor.fetchall(), backup_rows)
                    cursor.execute(f"SELECT snapshot_id FROM {migration.quoted(fixture)} ORDER BY snapshot_id")
                    self.assertEqual([row["snapshot_id"] for row in cursor.fetchall()], [2, 4, 5])
                connection.commit()
                self.assertEqual(migration.migrate(connection, config["database"], Path(directory))["status"], "already_migrated")

                with patch.object(pinchuang, "TARGET_TABLE", fixture):
                    adapter.test_connection()
                    for batch, count, hour in (("b3", 30, 2), ("b4", 40, 3)):
                        row = pinchuang.build_snapshot_row(
                            {"username": "author-1", "nickname": "测试作者"},
                            {"id": "v1", "description": "更新标题", "like_count": count,
                             "favorite_count": count + 1, "comment_count": count + 2,
                             "share_count": count + 3, "oss_video_url": "https://example.invalid/replacement.mp4"},
                            batch, synced_at=datetime(2026, 9, 3, hour),
                        )
                        self.assertEqual(adapter.write_snapshots([row]), 1)
                        connection.rollback()  # Refresh this observer's read snapshot.
                        with connection.cursor() as cursor:
                            cursor.execute(f"SELECT * FROM {migration.quoted(fixture)} ORDER BY snapshot_id")
                            rows = cursor.fetchall()
                        self.assertEqual(len(rows), 3)
                        updated = next(item for item in rows if item["snapshot_id"] == 2)
                        self.assertEqual(updated["sync_batch_id"], batch)
                        self.assertEqual(updated["synced_at"], datetime(2026, 9, 3, hour))
                        self.assertEqual(updated["video_url"], "https://example.invalid/v1.mp4")
                        self.assertEqual(updated["created_at"], datetime(2026, 9, 1))
                        self.assertEqual(updated["video_title"], "更新标题")
                        for field, expected in (("like_count", count), ("favorite_count", count + 1), ("comment_count", count + 2), ("share_count", count + 3)):
                            self.assertEqual(updated[field], expected)
                    self.assertEqual(adapter.latest_rows(["v1"])["v1"]["video_url"], "https://example.invalid/v1.mp4")
                    row["source_video_key"] = "v3"
                    self.assertEqual(adapter.write_snapshots([row]), 1)
                connection.rollback()
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) AS n FROM {migration.quoted(fixture)}")
                    self.assertEqual(cursor.fetchone()["n"], 4)
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute("SELECT TABLE_NAME AS name FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND LEFT(TABLE_NAME,%s)=%s", (config["database"], len(fixture), fixture))
                # Delete only tables created by this uniquely named synthetic fixture.
                for item in cursor.fetchall():
                    name = item["name"]
                    if name == fixture or name.startswith(fixture + "_"):
                        cursor.execute(f"DROP TABLE {migration.quoted(name)}")
            connection.close()


if __name__ == "__main__":
    unittest.main()
