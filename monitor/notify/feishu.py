"""Feishu (Lark) incoming-webhook notifier."""

from __future__ import annotations

from typing import Any

import requests

from monitor.errors import NotifyError
from monitor.models import Listing

_CARD_TITLE = "交易猫新品提醒"


class FeishuNotifier:
    """Send notifications through a Feishu custom-bot webhook."""

    name = "feishu"

    def __init__(self, webhook: str, timeout: float = 10):
        self.webhook = webhook
        self.timeout = timeout

    def send(self, items: list[Listing]) -> None:
        if not items:
            return
        elements = [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": _item_markdown(item)},
            }
            for item in items
        ]
        self._post(
            {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": _CARD_TITLE},
                    },
                    "elements": elements,
                },
            }
        )

    def send_text(self, text: str) -> None:
        self._post({"msg_type": "text", "content": {"text": text}})

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            response = requests.post(
                self.webhook, json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise NotifyError(f"feishu webhook request failed: {exc}") from exc

        if not 200 <= response.status_code < 300:
            raise NotifyError(
                f"feishu webhook returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise NotifyError("feishu webhook returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise NotifyError("feishu webhook returned an unexpected payload")

        code = data.get("code")
        if code != 0:
            message = data.get("msg", "")
            raise NotifyError(f"feishu webhook returned code {code}: {message}")


def _item_markdown(item: Listing) -> str:
    price = "价格未知" if item.price is None else f"¥{item.price}"
    details = (
        f"{price} · 服务器：{_extra_value(item, 'server')}"
        f" · 平台：{_extra_value(item, 'platform')}"
    )
    return f"**[{item.title}]({item.url})**\n{details}"


def _extra_value(item: Listing, key: str) -> str:
    value = item.extra.get(key)
    if value is None or value == "":
        return "未知"
    return str(value)
