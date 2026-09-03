"""Retired migration: video snapshots must remain unique within each sync batch.

This compatibility entry point rejects old commands before reading credentials
or accessing MySQL. Existing SQL backups and recovery tables are not changed.
"""

import argparse


RETIRED_MESSAGE = (
    "此迁移已停用：品创中枢按同步批次保留视频快照，"
    "唯一索引应为 (platform, source_video_key, sync_batch_id)。"
    "不能将历史快照合并为每个视频一行。"
    "已有正确三字段唯一索引的数据库无需迁移；"
    "如果曾迁移为两字段索引，请先备份并核对原迁移备份。"
)


def migrate(connection, schema, backup_root):
    """Reject programmatic use of the retired data-collapsing migration."""
    raise RuntimeError(RETIRED_MESSAGE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Accept the historical options solely to explain why they no longer run.
    parser.add_argument("--config")
    parser.add_argument("--backup-root")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.parse_args()
    parser.error(RETIRED_MESSAGE)


if __name__ == "__main__":
    main()
