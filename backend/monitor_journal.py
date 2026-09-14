"""Durable observations, upload receipts and paged failures, outside polling JSON."""
from contextlib import contextmanager
from collections import Counter
import json
from pathlib import Path
import sqlite3


class MonitorJournal:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS checkpoints (run_id TEXT, author_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(run_id,author_id))")
            connection.execute("CREATE TABLE IF NOT EXISTS results (run_id TEXT, author_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(run_id,author_id))")
            connection.execute("CREATE TABLE IF NOT EXISTS failures (run_id TEXT, author_id TEXT, ordinal INTEGER, payload TEXT NOT NULL, PRIMARY KEY(run_id,author_id,ordinal))")
            with connection:
                yield connection
        finally:
            connection.close()

    def checkpoint(self, run_id, author_id):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM checkpoints WHERE run_id=? AND author_id=?", (run_id, author_id)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_checkpoint(self, run_id, author_id, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (run_id, author_id, json.dumps(value, ensure_ascii=False)))

    def save_result(self, run_id, result):
        failures = result.get("failures", [])
        preview = [{k: v for k, v in f.items() if k not in {"error_trace", "attempt_errors"}} for f in failures[:3]]
        summary = {**result, "failures": preview, "failure_details_saved": len(failures),
                   "failure_stages": dict(Counter(f.get("stage", "unknown") for f in failures)),
                   "failures_truncated": len(failures) < int(result.get("failed_items", 0))}
        with self.connection() as db:
            author_id = result["author_id"]
            db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?)", (run_id, author_id, json.dumps(summary, ensure_ascii=False)))
            db.execute("DELETE FROM failures WHERE run_id=? AND author_id=?", (run_id, author_id))
            db.executemany("INSERT INTO failures VALUES (?,?,?,?)", [(run_id, author_id, index, json.dumps(f, ensure_ascii=False)) for index, f in enumerate(failures)])
        return summary

    def results(self, run_id):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM results WHERE run_id=? ORDER BY rowid", (run_id,))]

    def failures(self, run_id, author_id=None, *, offset=0, limit=50):
        where, args = "run_id=?", [run_id]
        if author_id is not None:
            where += " AND author_id=?"
            args.append(author_id)
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM failures WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT author_id,payload FROM failures WHERE " + where + " ORDER BY author_id,ordinal LIMIT ? OFFSET ?", [*args, limit, offset])
            return {"items": [{"author_id": r[0], **json.loads(r[1])} for r in rows], "total": total,
                    "offset": offset, "limit": limit, "has_more": offset + limit < total}
