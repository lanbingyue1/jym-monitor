"""Feishu (Lark) incoming-webhook notifier."""

from __future__ import annotations

from typing import Any

import requests

from monitor.errors import NotifyError
from monitor.models import Listing
from monitor.notify.base import published_text

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

    def send_text(self, text: str, *, subject: str | None = None) -> None:
        # Feishu text messages have no subject; the argument is accepted for
        # Notifier protocol compatibility.
        del subject
        self._post({"msg_type": "text", "content": {"text": text}})

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            response = requests.post(
                self.webhook, json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            # Exception text from requests may embed the webhook URL/token.
            raise NotifyError(
                f"feishu webhook request failed: {type(exc).__name__}"
            ) from exc

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
        if not isinstance(code, int):
            raise NotifyError("feishu webhook returned an unexpected error code")
        if code != 0:
            # Response messages are untrusted and may echo the webhook; expose
            # only the bounded numeric code.
            raise NotifyError(f"feishu webhook returned code {code}")


def _item_markdown(item: Listing) -> str:
    price = "价格未知" if item.price is None else f"¥{item.price}"
    lines = [f"**[{item.title}]({item.url})**"]
    published = published_text(item.published_at)
    if published:
        lines.append(f"上架时间：{published}")
    lines.append(
        f"{price} · 服务器：{_extra_value(item, 'server')}"
        f" · 平台：{_extra_value(item, 'platform')}"
    )
    return "\n".join(lines)


def _extra_value(item: Listing, key: str) -> str:
    value = item.extra.get(key)
    if value is None or value == "":
        return "未知"
    return str(value)
