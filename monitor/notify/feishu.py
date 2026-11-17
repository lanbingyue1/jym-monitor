"""Feishu (Lark) incoming-webhook notifier."""

from __future__ import annotations

import sys
import time
from typing import Any

import requests

from monitor.errors import NotifyError
from monitor.models import Listing
from monitor.notify.base import (
    extra_value,
    published_text,
    safe_report_lines,
    sell_points_text,
    seller_note,
)

_CARD_TITLE = "交易猫新品提醒"
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/image/create"
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_DOWNLOAD_ATTEMPTS = 2
_INLINE_IMAGE_LIMIT = 9
# Uploads are sequential HTTP round-trips; cap the whole card so a large burst
# of new listings cannot stall the workflow.
_INLINE_IMAGE_BUDGET = 18


class FeishuNotifier:
    """Send notifications through a Feishu custom-bot webhook.

    Inline images additionally require a self-built Feishu app (``app_id`` and
    ``app_secret``) because card images are referenced by ``img_key`` and only
    an app can upload them.  Without those credentials, images are delivered
    as links.
    """

    name = "feishu"

    def __init__(
        self,
        webhook: str,
        timeout: float = 10,
        app_id: str | None = None,
        app_secret: str | None = None,
    ):
        self.webhook = webhook
        self.timeout = timeout
        self._uploader = (
            FeishuImageUploader(app_id.strip(), app_secret.strip(), timeout)
            if app_id and app_secret
            else None
        )

    def send(self, items: list[Listing]) -> None:
        if not items:
            return
        elements: list[dict[str, Any]] = []
        budget = _INLINE_IMAGE_BUDGET
        for item in items:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _item_markdown(item)},
                }
            )
            image_elements = self._image_elements(
                item, min(_INLINE_IMAGE_LIMIT, budget)
            )
            budget -= sum(1 for e in image_elements if e.get("tag") == "img")
            elements.extend(image_elements)

        try:
            self._post(_card_payload(elements))
        except NotifyError as exc:
            if not any(e.get("tag") == "img" for e in elements):
                raise
            # An img_key rejected by the webhook would otherwise lose the whole
            # notification; retry once with the same content as links.  The
            # duplicate risk is only when the first POST actually landed.
            print(
                f"警告: 含内联图片的飞书卡片发送失败，改用链接重试: {exc}",
                file=sys.stderr,
            )
            self._post(_card_payload(self._link_elements(items)))

    def _link_elements(self, items: list[Listing]) -> list[dict[str, Any]]:
        """Build the same card content with every image as a link."""
        elements: list[dict[str, Any]] = []
        for item in items:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _item_markdown(item)},
                }
            )
            elements.extend(self._image_elements(item, 0))
        return elements

    def _image_elements(self, item: Listing, slots: int) -> list[dict[str, Any]]:
        """Render up to *slots* images inline, the rest as clickable links."""
        if not item.images:
            return []

        candidates = item.images[:max(0, slots)]
        keys = self._uploader.upload_all(candidates) if self._uploader else []
        elements: list[dict[str, Any]] = [
            {
                "tag": "img",
                "img_key": key,
                "alt": {"tag": "plain_text", "content": f"{item.title} 图{index}"},
            }
            for index, key in enumerate(keys, 1)
        ]

        remaining = item.images[len(keys):]
        if remaining:
            links = " ".join(
                f"[图{index}]({url})"
                for index, url in enumerate(remaining, len(keys) + 1)
            )
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"图片：{links}"},
                }
            )
        return elements

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


class FeishuImageUploader:
    """Upload listing images through a self-built Feishu app.

    Feishu cards reference images by ``img_key``, and only an app with
    ``im:resource`` permission can create those keys.  Uploads are best-effort:
    a failure is reported on stderr and the caller falls back to links.
    """

    def __init__(self, app_id: str, app_secret: str, timeout: float = 10):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self._token: str | None = None
        self._refresh_at = 0.0

    def upload_all(self, urls: tuple[str, ...]) -> list[str]:
        """Return the keys uploaded so far, stopping at the first failure."""
        keys: list[str] = []
        for index, url in enumerate(urls, 1):
            try:
                keys.append(self._upload(url))
            except NotifyError as exc:
                # Do not abort the whole notification for one image; the
                # remaining images are sent as links instead.
                print(
                    f"警告: 飞书图片 {index} 上传失败，改用链接展示: {exc}",
                    file=sys.stderr,
                )
                break
        return keys

    def _upload(self, url: str) -> str:
        content = self._download(url)
        token = self._access_token()
        try:
            response = requests.post(
                _IMAGE_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("image.jpg", content)},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NotifyError(
                f"feishu image upload failed: {type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise NotifyError(
                f"feishu image upload returned HTTP {response.status_code}"
            )
        payload = _json_payload(response, "feishu image upload")
        data = payload.get("data")
        key = data.get("image_key") if isinstance(data, dict) else None
        if not isinstance(key, str) or not key:
            raise NotifyError("feishu image upload returned no image_key")
        return key

    def _download(self, url: str) -> bytes:
        response = None
        error = NotifyError("image download failed")
        for _ in range(_DOWNLOAD_ATTEMPTS):
            try:
                response = requests.get(url, timeout=self.timeout)
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                # The image CDN occasionally drops cross-border connections;
                # one retry recovers most of these transient timeouts.
                error = NotifyError(
                    f"image download failed: {type(exc).__name__}"
                )
                response = None
            except requests.RequestException as exc:
                raise NotifyError(
                    f"image download failed: {type(exc).__name__}"
                ) from exc
        if response is None:
            raise error
        if not 200 <= response.status_code < 300:
            raise NotifyError(f"image download returned HTTP {response.status_code}")
        content = response.content
        if not content:
            raise NotifyError("image download returned an empty body")
        if len(content) > _MAX_UPLOAD_BYTES:
            raise NotifyError(
                f"image exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        return content

    def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._refresh_at:
            return self._token

        try:
            response = requests.post(
                _TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NotifyError(
                f"feishu app auth failed: {type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise NotifyError(
                f"feishu app auth returned HTTP {response.status_code}"
            )
        payload = _json_payload(
            response,
            "feishu app auth",
            hints={
                21001: "飞书安全策略拦截：请确认应用已发布启用、权限已审核通过，"
                "并检查安全设置中的 IP/访问限制"
            },
        )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise NotifyError("feishu app auth returned no token")

        expire = payload.get("expire")
        lifetime = expire if isinstance(expire, int) and expire > 0 else 7200
        self._token = token
        self._refresh_at = time.monotonic() + max(60, lifetime - 120)
        return token


def _card_payload(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": _CARD_TITLE},
            },
            "elements": elements,
        },
    }


def _json_payload(
    response, context: str, hints: dict[int, str] | None = None
) -> dict[str, Any]:
    """Decode a Feishu API response, requiring ``code == 0``."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise NotifyError(f"{context} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NotifyError(f"{context} returned an unexpected payload")
    code = payload.get("code")
    if not isinstance(code, int):
        raise NotifyError(f"{context} returned an unexpected error code")
    if code != 0:
        # Never echo the response message: it is untrusted.  Local hints for
        # known codes are safe and let the log name the likely cause.
        hint = (hints or {}).get(code)
        suffix = f"（{hint}）" if hint else ""
        raise NotifyError(f"{context} returned code {code}{suffix}")
    return payload


def _item_markdown(item: Listing) -> str:
    price = "价格未知" if item.price is None else f"¥{item.price}"
    lines = [f"**[{item.title}]({item.url})**"]
    lines.append(
        f"{price} · 账号：{extra_value(item, 'account')}"
        f" · 平台：{extra_value(item, 'platform')}"
        f" · 服务器：{extra_value(item, 'server')}"
    )
    published = published_text(item.published_at)
    if published:
        lines.append(f"上架时间：{published}")
    points = sell_points_text(item)
    if points:
        lines.append(f"特点：{points}")
    note = seller_note(item)
    if note:
        lines.append(f"卖家说：{note}")
    lines.extend(safe_report_lines(item.safe_report))
    return "\n".join(lines)
