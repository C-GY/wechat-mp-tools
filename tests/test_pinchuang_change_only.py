"""Change-only upserts against disposable MySQL TEMPORARY tables.

Run explicitly with PINCHUANG_MYSQL_TEST_CONFIG pointing at the hub config.
Only the live table's schema is read. All test data vanishes on disconnect.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(os.environ.get("PINCHUANG_MYSQL_TEST_CONFIG"), "Explicit MySQL integration config required")
class ChangeOnlyMySQLTests(unittest.TestCase):
    def setUp(self):
        from backend import pinchuang

        self.pinchuang = pinchuang
        config = json.loads(Path(os.environ["PINCHUANG_MYSQL_TEST_CONFIG"]).read_text(encoding="utf-8-sig"))["database"]
        self.adapter = pinchuang.MySQLSnapshotAdapter(config)
        self.connection = self.adapter.connect()
        self.addCleanup(self.connection.close)
        self.table = "pc_change_test_" + uuid4().hex[:8]
        with self.connection.cursor() as cursor:
            cursor.execute(f"CREATE TEMPORARY TABLE `{self.table}` LIKE `{pinchuang.TARGET_TABLE}`")
        for patcher in (
            patch.object(pinchuang, "TARGET_TABLE", self.table),
            patch.object(self.adapter, "connect", return_value=self.connection),
            patch.object(self.connection, "close"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self, key="v1", batch="b1", hour=1):
        return self.pinchuang.build_snapshot_row(
            {"username": "author-1", "nickname": "作者Creator", "head_img_url": "https://example.invalid/avatar-1"},
            {"id": key, "description": "Title Café", "createtime": 1721188800,
             "duration_seconds": 10, "like_count": 10, "share_count": 2,
             "favorite_count": 3, "comment_count": 4,
             "video_url": "https://example.invalid/source?token=old",
             "oss_video_url": "https://example.invalid/stored.mp4"},
            batch, synced_at=datetime(2026, 9, 3, hour),
        )

    def stored(self, key="v1"):
        with self.connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{self.table}` WHERE platform=%s AND source_video_key=%s", ("wechat_channels", key))
            return cursor.fetchone()

    def test_identical_business_data_preserves_entire_row_across_batches(self):
        self.adapter.write_snapshots([self.row()])
        before = self.stored()
        for batch, hour in (("b2", 2), ("b3", 3)):
            with self.subTest(batch=batch):
                self.adapter.write_snapshots([self.row(batch=batch, hour=hour)])
                self.assertEqual(self.stored(), before)

    def test_temporary_links_and_raw_metadata_alone_do_not_trigger_update(self):
        self.adapter.write_snapshots([self.row()])
        before = self.stored()
        incoming = self.row(batch="b2", hour=2)
        payload = json.loads(incoming["raw_payload"])
        payload["video"].update({"video_url": "https://example.invalid/source?token=new", "collected_at": "2026-09-03 02:00:00"})
        payload["author"]["head_img_url"] = "https://example.invalid/avatar-2"
        incoming["raw_payload"] = json.dumps(payload)
        incoming["video_url"] = "https://example.invalid/must-not-replace.mp4"
        self.adapter.write_snapshots([incoming])
        self.assertEqual(self.stored(), before)

    def test_each_business_field_change_updates_markers_and_keeps_oss_url(self):
        changes = {
            "external_video_id": "different-external-id",
            "author_id": "different-author-id",
            "author_name": "作者creator",  # Case-only change must not use ai_ci equality.
            "video_title": "title Café",
            "published_at": datetime(2026, 9, 1),
            "duration_ms": 11000,
            "like_count": 0,  # Decreasing metrics are still changes.
            "share_count": 3,
            "favorite_count": 4,
            "comment_count": 5,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                key = "change-" + field
                self.adapter.write_snapshots([self.row(key)])
                before = self.stored(key)
                incoming = {**self.row(key, "b2", 2), field: value,
                            "video_url": "https://example.invalid/must-not-replace.mp4",
                            "raw_payload": json.dumps({"changed_field": field})}
                self.adapter.write_snapshots([incoming])
                after = self.stored(key)
                self.assertEqual(after[field], value)
                self.assertEqual(after["sync_batch_id"], "b2")
                self.assertEqual(after["synced_at"], incoming["synced_at"])
                self.assertEqual(json.loads(after["raw_payload"]), {"changed_field": field})
                for immutable in ("snapshot_id", "created_at", "video_url"):
                    self.assertEqual(after[immutable], before[immutable])
                # Once that changed value is stored, retrying it is a no-op.
                self.adapter.write_snapshots([{**incoming, "sync_batch_id": "b3", "synced_at": datetime(2026, 9, 3, 3)}])
                self.assertEqual(self.stored(key), after)

    def test_null_zero_and_null_text_transitions_are_compared_safely(self):
        for field, values in (
            ("comment_count", (None, None, 0, 0, None)),
            ("video_title", (None, None, "", "", "Café", "Cafe", "Cafe ")),
        ):
            previous = None
            for index, value in enumerate(values):
                with self.subTest(field=field, index=index):
                    row = {**self.row(field, f"b{index}", index + 1), field: value}
                    self.adapter.write_snapshots([row])
                    current = self.stored(field)
                    if previous is not None and previous[field] == value:
                        self.assertEqual(current, previous)
                    else:
                        self.assertEqual(current["sync_batch_id"], row["sync_batch_id"])
                        self.assertEqual(current["synced_at"], row["synced_at"])
                        self.assertEqual(current[field], value)
                    previous = current

    def test_mixed_bulk_write_and_submillisecond_input_do_not_create_false_updates(self):
        first = self.row("unchanged")
        second = self.row("changed")
        self.adapter.write_snapshots([first, second])
        unchanged = self.stored("unchanged")
        changed = {**self.row("changed", "b2", 2), "like_count": 11}
        new = self.row("new", "b2", 2)
        self.adapter.write_snapshots([self.row("unchanged", "b2", 2), changed, new])
        self.assertEqual(self.stored("unchanged"), unchanged)
        self.assertEqual(self.stored("changed")["like_count"], 11)
        self.assertEqual(self.stored("changed")["sync_batch_id"], "b2")
        self.assertEqual(self.stored("new")["sync_batch_id"], "b2")
        with self.connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS n FROM `{self.table}`")
            self.assertEqual(cursor.fetchone()["n"], 3)
        # MySQL DATETIME(3) precision, not Python's 6-digit precision, determines equality.
        row = self.row("precision")
        row["published_at"] = row["published_at"] + timedelta(microseconds=100)
        self.adapter.write_snapshots([row])
        before = self.stored("precision")
        self.adapter.write_snapshots([{**row, "sync_batch_id": "b2", "synced_at": datetime(2026, 9, 3, 2)}])
        self.assertEqual(self.stored("precision"), before)


if __name__ == "__main__":
    unittest.main()
