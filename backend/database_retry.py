"""Retry whole idempotent operations using a fresh connection each time."""
from functools import wraps
import time

import pymysql


DATABASE_RETRY_DELAYS = (0.25, 1)


def retry_database_operation(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        for attempt in range(len(DATABASE_RETRY_DELAYS) + 1):
            checkpoint = getattr(args[0], "retry_checkpoint", None)
            if callable(checkpoint):
                checkpoint()
            try:
                return operation(*args, **kwargs)
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
                code = exc.args[0] if exc.args else None
                if code not in {0, 1205, 1213, 2006, 2013, 2055} or attempt == len(DATABASE_RETRY_DELAYS):
                    exc.add_note(f"数据库操作尝试 {attempt + 1} 次；每次使用新连接和完整事务")
                    raise
                time.sleep(DATABASE_RETRY_DELAYS[attempt])
    return call
