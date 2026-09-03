"""Explicit, backed-up migration from batch snapshots to one row per video.

Default mode is read-only. Stop all writers before --apply --writers-stopped.
The original InnoDB table is retained under a timestamped backup name; a local
SQL recovery copy is flushed to disk before an atomic two-table rename.
This script deliberately does not import the application or start its scheduler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pymysql


TARGET = "competitor_video_snapshots"
IDENTITY = {"platform", "source_video_key"}
LEGACY_IDENTITY = IDENTITY | {"sync_batch_id"}


def quoted(name):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        raise ValueError("Unsafe SQL identifier")
    return f"`{name}`"


def unique_indexes(cursor):
    cursor.execute(f"SHOW INDEX FROM {quoted(TARGET)}")
    indexes = {}
    for row in cursor.fetchall():
        if not row["Non_unique"]:
            indexes.setdefault(row["Key_name"], []).append(row)
    return indexes


def matches_index(parts, columns):
    return (
        len(parts) == len(columns)
        and {part["Column_name"] for part in parts} == columns
        and all(part.get("Sub_part") is None for part in parts)
    )


def inspect(cursor, schema):
    cursor.execute("SELECT VERSION() AS version")
    version = cursor.fetchone()["version"]
    numbers = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not numbers or not version.startswith("8.") or tuple(map(int, numbers.groups())) < (8, 0, 13):
        raise RuntimeError("Migration requires MySQL 8.0.13 or newer (MySQL 8 only)")
    cursor.execute(
        "SELECT ENGINE AS engine FROM information_schema.tables "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, TARGET),
    )
    table = cursor.fetchone()
    if not table or table["engine"] != "InnoDB":
        raise RuntimeError("Target must be an existing InnoDB table")
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.key_column_usage WHERE "
        "(REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s) OR "
        "(TABLE_SCHEMA=%s AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL)",
        (schema, TARGET, schema, TARGET),
    )
    if cursor.fetchone()["n"]:
        raise RuntimeError("Foreign keys require a separately reviewed migration")
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.triggers "
        "WHERE EVENT_OBJECT_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s", (schema, TARGET),
    )
    if cursor.fetchone()["n"]:
        raise RuntimeError("Triggers require a separately reviewed migration")
    cursor.execute(f"SHOW FULL COLUMNS FROM {quoted(TARGET)}")
    definitions = cursor.fetchall()
    required = IDENTITY | {"snapshot_id", "sync_batch_id", "synced_at", "video_url"}
    if not required.issubset({row["Field"] for row in definitions}):
        raise RuntimeError("Target is missing required video columns")
    if any("GENERATED" in row["Extra"] and "DEFAULT_GENERATED" not in row["Extra"] for row in definitions):
        raise RuntimeError("Generated columns require a separately reviewed migration")
    if any(row["Null"] != "NO" for row in definitions if row["Field"] in IDENTITY):
        raise RuntimeError("Video identity columns must be NOT NULL")
    indexes = unique_indexes(cursor)
    if not matches_index(indexes.get("PRIMARY", []), {"snapshot_id"}):
        raise RuntimeError("Expected snapshot_id primary key")
    legacy_indexes = []
    already_unique = False
    for name, parts in indexes.items():
        if name == "PRIMARY":
            continue
        if matches_index(parts, IDENTITY):
            already_unique = True
        elif matches_index(parts, LEGACY_IDENTITY):
            legacy_indexes.append(name)
        else:
            raise RuntimeError("Unexpected unique index; review schema before migrating")
    cursor.execute(
        f"SELECT COUNT(*) AS row_count, "
        "COUNT(DISTINCT platform, source_video_key) AS video_count, "
        "COALESCE(SUM(platform IS NULL OR TRIM(platform)='' OR "
        "source_video_key IS NULL OR TRIM(source_video_key)=''),0) AS invalid_keys "
        f"FROM {quoted(TARGET)}"
    )
    counts = cursor.fetchone()
    if counts["invalid_keys"]:
        raise RuntimeError("Invalid video identities must be repaired before migrating")
    cursor.execute(f"SHOW CREATE TABLE {quoted(TARGET)}")
    ddl = cursor.fetchone()["Create Table"]
    return {
        "version": version, "row_count": int(counts["row_count"]),
        "video_count": int(counts["video_count"]), "already_unique": already_unique,
        "legacy_indexes": legacy_indexes, "columns": [row["Field"] for row in definitions],
        "ddl": ddl,
    }


def latest_select(columns):
    names = ",".join(map(quoted, columns))
    # Ranking is done by MySQL so identity equality uses the table's collation.
    return (
        f"SELECT {names} FROM (SELECT {names}, ROW_NUMBER() OVER ("
        "PARTITION BY platform, source_video_key "
        "ORDER BY synced_at DESC, snapshot_id DESC) AS migration_rank "
        f"FROM {quoted(TARGET)}) AS ranked WHERE migration_rank=1 ORDER BY snapshot_id"
    )


def write_backup(cursor, directory, info, rows, recovery_table):
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "full_backup.sql"
    columns = info["columns"]
    names = ",".join(map(quoted, columns))
    insert = f"INSERT INTO {quoted(recovery_table)} ({names}) VALUES ({','.join(['%s'] * len(columns))})"
    ddl = info["ddl"].replace(f"CREATE TABLE {quoted(TARGET)}", f"CREATE TABLE {quoted(recovery_table)}", 1)
    cursor.execute("SELECT @@SESSION.sql_mode AS sql_mode")
    sql_mode = cursor.fetchone()["sql_mode"]
    with path.open("x", encoding="utf-8", newline="\n") as backup:
        backup.write("-- Full recovery copy. Imports into its own recovery table, never the active table.\n")
        backup.write("SET NAMES utf8mb4;\nSET time_zone = '+08:00';\n")
        backup.write(cursor.mogrify("SET SESSION sql_mode = %s;\n", (sql_mode,)))
        backup.write(ddl + ";\nSTART TRANSACTION;\n")
        for row in rows:
            backup.write(cursor.mogrify(insert, tuple(row[name] for name in columns)) + ";\n")
        backup.write("COMMIT;\n")
        backup.flush()
        os.fsync(backup.fileno())
    return {"sql_backup": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write_manifest(directory, manifest):
    with (directory / "manifest.json").open("w", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())


def migrate(connection, schema, backup_root):
    with connection.cursor() as cursor:
        info = inspect(cursor, schema)
        if info["already_unique"]:
            return {"status": "already_migrated", "row_count": info["row_count"]}
        token = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
        staging = f"{TARGET}_stage_{token}"
        remote_backup = f"{TARGET}_backup_{token}"
        recovery = f"{TARGET}_recovery_{token}"
        directory = backup_root / token
        manifest = {
            "status": "preparing", "schema": schema, "active_table": TARGET,
            "backup_table": remote_backup, "staging_table": staging,
            "recovery_table": recovery,
        }
        # Do all DDL on the empty staging table. The source remains untouched.
        cursor.execute(f"CREATE TABLE {quoted(staging)} LIKE {quoted(TARGET)}")
        alterations = [f"DROP INDEX {quoted(name)}" for name in info["legacy_indexes"]]
        alterations.append("ADD UNIQUE KEY `uq_competitor_video` (`platform`,`source_video_key`)")
        cursor.execute(f"ALTER TABLE {quoted(staging)} " + ", ".join(alterations))
        locked = False
        try:
            cursor.execute(f"LOCK TABLES {quoted(TARGET)} WRITE, {quoted(staging)} WRITE")
            locked = True
            # Recheck under the lock; concurrent writes before the lock are included.
            locked_info = inspect(cursor, schema)
            if info["ddl"] != locked_info["ddl"]:
                raise RuntimeError("Source schema changed during preparation; original table unchanged")
            info = locked_info
            cursor.execute(f"SELECT * FROM {quoted(TARGET)} ORDER BY snapshot_id")
            original_rows = cursor.fetchall()
            manifest.update(write_backup(cursor, directory, info, original_rows, recovery))
            manifest.update({"original_rows": len(original_rows), "unique_videos": info["video_count"]})
            manifest["status"] = "backed_up"
            write_manifest(directory, manifest)

            selection = latest_select(info["columns"])
            cursor.execute(selection)
            expected_rows = cursor.fetchall()
            names = ",".join(map(quoted, info["columns"]))
            cursor.execute(f"INSERT INTO {quoted(staging)} ({names}) " + selection)
            cursor.execute(f"SELECT * FROM {quoted(staging)} ORDER BY snapshot_id")
            actual_rows = cursor.fetchall()
            if len(actual_rows) != info["video_count"] or actual_rows != expected_rows:
                raise RuntimeError("Latest-row verification failed; original table unchanged")
            connection.commit()
            manifest["status"] = "verified_before_swap"
            write_manifest(directory, manifest)
            # MySQL 8.0.13+ permits atomic multi-table RENAME under WRITE locks.
            # No DELETE or DROP of source data: the entire source becomes backup.
            cursor.execute(
                f"RENAME TABLE {quoted(TARGET)} TO {quoted(remote_backup)}, "
                f"{quoted(staging)} TO {quoted(TARGET)}"
            )
            cursor.execute(f"SELECT * FROM {quoted(TARGET)} ORDER BY snapshot_id")
            if cursor.fetchall() != expected_rows:
                raise RuntimeError("Post-swap verification failed; full original remains in backup_table")
            cursor.execute(f"SELECT * FROM {quoted(remote_backup)} ORDER BY snapshot_id")
            if cursor.fetchall() != original_rows:
                raise RuntimeError("Remote backup verification failed; use full_backup.sql")
            manifest["status"] = "completed"
            manifest["active_rows"] = len(expected_rows)
            manifest["historical_rows_removed_from_active"] = len(original_rows) - len(expected_rows)
            write_manifest(directory, manifest)
            return manifest
        except Exception:
            connection.rollback()
            # Do not delete partial staging/backup artifacts, or undo an uncertain
            # rename. The manifest and table names allow explicit recovery.
            raise
        finally:
            if locked:
                cursor.execute("UNLOCK TABLES")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Existing hub config JSON (never printed)")
    parser.add_argument("--backup-root", type=Path, default=Path(__file__).resolve().parents[1] / "data/backups/pinchuang_single_video")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true", help="Confirm all application writers are stopped")
    args = parser.parse_args()
    if args.apply and not args.writers_stopped:
        parser.error("Stop all application writers first, then supply --writers-stopped")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))["database"]
    connection = pymysql.connect(
        host=config["host"], port=int(config.get("port", 3306)), user=config["username"],
        password=config["password"], database=config["database"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        connect_timeout=10, read_timeout=45, write_timeout=45,
        init_command="SET time_zone = '+08:00'",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION lock_wait_timeout=20")
            cursor.execute("SET SESSION innodb_lock_wait_timeout=20")
        if args.apply:
            result = migrate(connection, config["database"], args.backup_root)
        else:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                result = inspect(cursor, config["database"])
                result.pop("ddl")
            connection.rollback()
            result["status"] = "read_only_preview"
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
