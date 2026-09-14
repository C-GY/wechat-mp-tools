"""Structured, redacted failures shared by the capture/transfer/store pipeline."""
import re
import traceback

import requests


def redact_error(value):
    text = str(value)
    text = re.sub(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]*", r"\1?<REDACTED>", text)
    text = re.sub(r"(?i)((?:authorization|password|access_key_secret|token|signature)\s*[:=]\s*)[^\s,;]+",
                  r"\1<REDACTED>", text)
    return text


def error_details(exc, stage):
    details = {"error": redact_error(exc), "error_type": type(exc).__name__, "stage": stage,
               "error_trace": redact_error("".join(traceback.format_exception(exc)))[-12000:]}
    code = getattr(exc, "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        code = exc.response.status_code
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    if code is not None:
        details["error_code"] = code
    return details


class TransferHTTPError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class IncompleteDownload(requests.exceptions.ChunkedEncodingError):
    pass


def transient_transfer_error(exc):
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    code = getattr(exc, "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        code = exc.response.status_code
    return code in {408, 429, 500, 502, 503, 504}
