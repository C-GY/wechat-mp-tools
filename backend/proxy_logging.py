"""Bounded, redacted diagnostics that also work in the windowless desktop build."""
import logging
import threading
from logging.handlers import RotatingFileHandler

from backend.sync_errors import redact_error

_lock = threading.Lock()
_handler = None
_logger = logging.getLogger("channels.proxy")
_logger.setLevel(logging.INFO)
_logger.propagate = False


class _DiagnosticFilter(logging.Filter):
    def filter(self, record):
        return record.name == "channels.proxy" or (
            record.levelno >= logging.WARNING
            and (record.name.startswith("mitmproxy.") or record.name == "asyncio")
        )


class _RedactedFormatter(logging.Formatter):
    def format(self, record):
        return redact_error(super().format(record))[:12000]


def runtime_logger(data_dir):
    global _handler
    path = (data_dir / "proxy_runtime.log").resolve()
    with _lock:
        if _handler is None or _handler.baseFilename != str(path):
            if _handler is not None:
                logging.getLogger().removeHandler(_handler)
                _logger.removeHandler(_handler)
                _handler.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            _handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3,
                                           encoding="utf-8")
            _handler.addFilter(_DiagnosticFilter())
            _handler.setFormatter(_RedactedFormatter(
                "%(asctime)s %(levelname)s pid=%(process)d %(threadName)s %(name)s: %(message)s"
            ))
            _logger.addHandler(_handler)
            logging.getLogger().addHandler(_handler)
    return _logger
