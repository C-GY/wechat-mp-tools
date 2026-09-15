"""Durable successful-upload receipts, independent of capture and task history."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time


class UploadReceipts:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS uploads (
                        endpoint TEXT NOT NULL, bucket TEXT NOT NULL,
                        prefix TEXT NOT NULL, video_id TEXT NOT NULL,
                        object_key TEXT NOT NULL, url TEXT NOT NULL,
                        size INTEGER NOT NULL, etag TEXT NOT NULL,
                        recorded_at INTEGER NOT NULL,
                        PRIMARY KEY (endpoint, bucket, prefix, video_id)
                    )
                """)
                yield connection
        finally:
            connection.close()

    def get(self, endpoint, bucket, prefix, video_id):
        if not isinstance(bucket, str) or not bucket or not self.path.exists():
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT bucket, object_key, url, size, etag FROM uploads "
                "WHERE endpoint=? AND bucket=? AND prefix=? AND video_id=?",
                (endpoint, bucket, prefix, video_id),
            ).fetchone()
        return dict(row) if row else None

    def save(self, endpoint, prefix, video_id, result):
        self.save_many(endpoint, prefix, [(video_id, result)], replace=True)

    def save_many(self, endpoint, prefix, records, *, replace=False):
        """Import old successes without overwriting a newer verified receipt."""
        rows = []
        for video_id, result in records:
            if not all(isinstance(value, str) and value for value in (
                video_id, result.get("bucket"), result.get("object_key"), result.get("url"),
            )):
                continue
            rows.append((endpoint, result["bucket"], prefix, video_id,
                         result["object_key"], result["url"], int(result.get("size") or 0),
                         str(result.get("etag") or ""), int(time.time())))
        if not rows:
            return
        conflict = ("DO UPDATE SET object_key=excluded.object_key, url=excluded.url, "
                    "size=excluded.size, etag=excluded.etag, recorded_at=excluded.recorded_at") if replace else "DO NOTHING"
        with self._connection() as connection:
            connection.executemany(
                "INSERT INTO uploads (endpoint, bucket, prefix, video_id, object_key, url, size, etag, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint, bucket, prefix, video_id) " + conflict,
                rows,
            )
