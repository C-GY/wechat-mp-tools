"""Batch snapshot behavior on disposable MySQL TEMPORARY tables.

Run with PINCHUANG_MYSQL_TEST_CONFIG pointing at the hub config.
Only the live schema is read; fixture data vanishes on disconnect.
"""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(os.environ.get("PINCHUANG_MYSQL_TEST_CONFIG"), "Explicit MySQL integration config required")
class BatchSnapshotMySQLTests(unittest.TestCase):
    def setUp(self):
        from backend import pinchuang

        self.pinchuang = pinchuang
        config = json.loads(Path(os.environ["PINCHUANG_MYSQL_TEST_CONFIG"]).read_text(encoding="utf-8-sig"))["database"]
        self.adapter = pinchuang.MySQLSnapshotAdapter(config)
        self.connection = self.adapter.connect()
        self.addCleanup(self.connection.close)
        self.table = "pc_batch_test_" + uuid4().hex[:8]
        with self.connection.cursor() as cursor:
            cursor.execute(f"CREATE TEMPORARY TABLE `{self.table}` LIKE `{pinchuang.TARGET_TABLE}`")
            # Keep the fixture contract independent of the live table's indexes.
            cursor.execute(f"SHOW INDEX FROM `{self.table}`")
            names = {row["Key_name"] for row in cursor.fetchall() if not row["Non_unique"] and row["Key_name"] != "PRIMARY"}
            for name in names:
                cursor.execute(f"ALTER TABLE `{self.table}` DROP INDEX `{name.replace('`', '``')}`")
            cursor.execute(f"ALTER TABLE `{self.table}` ADD UNIQUE KEY uq_batch (platform, source_video_key, sync_batch_id)")
        for patcher in (
            patch.object(pinchuang, "TARGET_TABLE", self.table),
            patch.object(self.adapter, "connect", return_value=self.connection),
            patch.object(self.connection, "close"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self, key="v1", batch="b1", hour=1):
        return self.pinchuang.build_snapshot_row(
            {"username": "author-1", "nickname": "作者Creator"},
            {"id": key, "description": "Title Café", "createtime": 1721188800,
             "duration_seconds": 10, "like_count": 10, "share_count": 2,
             "favorite_count": 3, "comment_count": 4,
             "oss_video_url": "https://example.invalid/stored.mp4"},
            batch, synced_at=datetime(2026, 9, 3, hour),
        )

    def stored(self, key="v1", batch="b1"):
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{self.table}` WHERE platform=%s AND source_video_key=%s AND sync_batch_id=%s",
                ("wechat_channels", key, batch),
            )
            return cursor.fetchone()

    def count(self):
        with self.connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS n FROM `{self.table}`")
            return cursor.fetchone()["n"]

    def test_new_batches_preserve_history_even_when_business_data_is_unchanged(self):
        self.adapter.write_snapshots([self.row()])
        original = self.stored()
        for batch, hour in (("b2", 2), ("b3", 3)):
            self.adapter.write_snapshots([self.row(batch=batch, hour=hour)])
            snapshot = self.stored(batch=batch)
            self.assertEqual(snapshot["sync_batch_id"], batch)
            self.assertEqual(snapshot["synced_at"], datetime(2026, 9, 3, hour))
            self.assertNotEqual(snapshot["snapshot_id"], original["snapshot_id"])
            self.assertEqual(self.stored(), original)
        self.assertEqual(self.count(), 3)

    def test_same_batch_retry_refreshes_time_and_payload_without_replacing_oss(self):
        self.adapter.write_snapshots([self.row(), self.row(batch="b2", hour=2)])
        history = self.stored()
        before = self.stored(batch="b2")
        incoming = {**self.row(batch="b2", hour=3),
                    "raw_payload": json.dumps({"refreshed": True}),
                    "video_url": "https://example.invalid/must-not-replace.mp4"}
        self.adapter.write_snapshots([incoming])
        after = self.stored(batch="b2")
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.stored(), history)
        self.assertEqual(after["synced_at"], incoming["synced_at"])
        self.assertEqual(json.loads(after["raw_payload"]), {"refreshed": True})
        for name in ("snapshot_id", "created_at", "video_url", "sync_batch_id"):
            self.assertEqual(after[name], before[name])

    def test_changed_statistics_and_nulls_update_only_the_retried_batch(self):
        self.adapter.write_snapshots([self.row(), self.row(batch="b2", hour=2)])
        history = self.stored()
        before = self.stored(batch="b2")
        changes = {"like_count": 0, "comment_count": None, "video_title": None,
                   "author_name": "作者creator", "share_count": 7}
        incoming = {**self.row(batch="b2", hour=3), **changes}
        self.adapter.write_snapshots([incoming])
        after = self.stored(batch="b2")
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.stored(), history)
        self.assertEqual(after["snapshot_id"], before["snapshot_id"])
        for name, value in changes.items():
            self.assertEqual(after[name], value)

    def test_latest_rows_uses_time_then_primary_key_and_stays_platform_scoped(self):
        rows = [
            {**self.row(batch="b1", hour=3), "video_url": "https://example.invalid/first.mp4"},
            {**self.row(batch="b2", hour=1), "video_url": "https://example.invalid/older.mp4"},
            {**self.row(batch="b3", hour=3), "video_url": "https://example.invalid/latest.mp4"},
            {**self.row(batch="b4", hour=4), "platform": "other_platform", "video_url": "https://example.invalid/other.mp4"},
            self.row("v2"),
        ]
        self.adapter.write_snapshots(rows)
        latest = self.adapter.latest_rows(["v1", "v2", "v1", "missing"])
        self.assertEqual(set(latest), {"v1", "v2"})
        self.assertEqual(latest["v1"]["video_url"], rows[2]["video_url"])
        self.assertEqual(latest["v1"]["synced_at"], rows[2]["synced_at"])
        self.assertEqual(self.adapter.latest_rows([]), {})

    def test_mixed_bulk_write_keeps_earlier_batches_and_adds_all_current_snapshots(self):
        self.adapter.write_snapshots([self.row("unchanged"), self.row("changed")])
        history = {key: self.stored(key) for key in ("unchanged", "changed")}
        rows = [self.row("unchanged", "b2", 2),
                {**self.row("changed", "b2", 2), "like_count": 11},
                self.row("new", "b2", 2)]
        self.assertEqual(self.adapter.write_snapshots(rows), 3)
        self.assertEqual(self.count(), 5)
        for key, original in history.items():
            self.assertEqual(self.stored(key), original)
        self.assertEqual(self.stored("changed", "b2")["like_count"], 11)
        self.assertEqual(self.stored("unchanged", "b2")["sync_batch_id"], "b2")
        self.assertEqual(self.stored("new", "b2")["sync_batch_id"], "b2")
        self.adapter.write_snapshots(rows)
        self.assertEqual(self.count(), 5)


if __name__ == "__main__":
    unittest.main()
