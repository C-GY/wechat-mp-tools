"""Shared Feishu delivery; importing this module starts no business services."""
import base64
import hashlib
import hmac
import time
from typing import Any
import requests


class FeishuNotifier:
    def __init__(self, webhook_url: str, secret: str = "", session=None):
        self.webhook_url = str(webhook_url or "").strip()
        self.secret = str(secret or "").strip()
        self.session = session or requests.Session()

    def configured(self) -> bool:
        return bool(self.webhook_url)

    @staticmethod
    def _card_markdown(message: str) -> str:
        lines = []
        for line in str(message or "").splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            if "：" in stripped:
                label, value = stripped.split("：", 1)
                lines.append(f"**{label}：** {value.strip()}")
            else:
                lines.append(stripped)
        return "\n".join(lines)

    def _payload(self, title: str, message: str) -> dict:
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"【{title}】"}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": self._card_markdown(message)}}],
            },
        }
        if self.secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{self.secret}".encode("utf-8")
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()).decode("utf-8")
        return payload

    def send(self, title: str, message: str, attempts: int = 3) -> dict:
        if not self.configured():
            return {"sent": False, "skipped": True, "reason": "未配置飞书机器人"}
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = self.session.post(self.webhook_url, json=self._payload(title, message), timeout=(5, 10))
                response.raise_for_status()
                body = response.json()
                code = body.get("code", body.get("StatusCode", 0))
                if code not in (0, "0", None):
                    raise RuntimeError(body.get("msg") or body.get("StatusMessage") or str(body))
                return {"sent": True, "attempts": attempt}
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    time.sleep(attempt)
        return {"sent": False, "attempts": attempts, "error": last_error}
