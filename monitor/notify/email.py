"""Email notifier built on the standard library ``smtplib``/``email``."""

from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage

from monitor.errors import NotifyError
from monitor.models import Listing
from monitor.notify.base import (
    extra_value,
    published_text,
    safe_report_lines,
    sell_points_text,
    seller_note,
)

SMTP_TIMEOUT = 20
ALERT_SUBJECT = "[监控异常] 交易猫监控"


class EmailNotifier:
    """Send notifications over SMTP."""

    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        to: str,
        use_ssl: bool = True,
        include_images: bool = True,
        include_safe_report: bool = True,
    ):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.to = _parse_addresses(to)
        self.use_ssl = use_ssl
        self.include_images = include_images
        self.include_safe_report = include_safe_report

    def send(self, items: list[Listing]) -> None:
        if not items:
            return
        message = EmailMessage()
        message["Subject"] = f"交易猫新品提醒 ({len(items)})"
        message["From"] = self.user
        message["To"] = ", ".join(self.to)
        message.set_content(
            _plain_body(items, self.include_images, self.include_safe_report)
        )
        message.add_alternative(
            _html_body(items, self.include_images, self.include_safe_report),
            subtype="html",
        )
        self._deliver(message)

    def send_text(self, text: str, *, subject: str | None = None) -> None:
        message = EmailMessage()
        message["Subject"] = subject or ALERT_SUBJECT
        message["From"] = self.user
        message["To"] = ", ".join(self.to)
        message.set_content(text)
        self._deliver(message)

    def _deliver(self, message: EmailMessage) -> None:
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(
                    self.host, self.port, timeout=SMTP_TIMEOUT
                ) as smtp:
                    smtp.login(self.user, self.password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(
                    self.host, self.port, timeout=SMTP_TIMEOUT
                ) as smtp:
                    smtp.starttls()
                    smtp.login(self.user, self.password)
                    smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # Never interpolate the exception text or the recipient list: both
            # can contain addresses and flow into public logs/summaries.
            detail = type(exc).__name__
            smtp_code = getattr(exc, "smtp_code", None)
            if isinstance(smtp_code, int):
                detail = f"{detail} (SMTP code {smtp_code})"
            raise NotifyError(f"email delivery failed: {detail}") from exc


def _parse_addresses(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _price_text(item: Listing) -> str:
    return "价格未知" if item.price is None else f"¥{item.price}"


def _plain_body(
    items: list[Listing],
    include_images: bool = True,
    include_safe_report: bool = True,
) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(item.title)
        lines.append(f"价格：{_price_text(item)}")
        published = published_text(item.published_at)
        if published:
            lines.append(f"上架时间：{published}")
        lines.append(
            f"账号：{extra_value(item, 'account')}"
            f" · 平台：{extra_value(item, 'platform')}"
            f" · 服务器：{extra_value(item, 'server')}"
        )
        points = sell_points_text(item)
        if points:
            lines.append(f"特点：{points}")
        note = seller_note(item)
        if note:
            lines.append(f"卖家说：{note}")
        if include_safe_report:
            lines.extend(safe_report_lines(item.safe_report))
        if include_images:
            for index, url in enumerate(item.images, 1):
                lines.append(f"图片{index}：{url}")
        lines.append(item.url)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _html_body(
    items: list[Listing],
    include_images: bool = True,
    include_safe_report: bool = True,
) -> str:
    rows: list[str] = []
    details: list[str] = []
    # Only add the 上架时间 column when at least one item has a publication
    # time; unknown times render as an empty cell rather than a placeholder.
    show_published = any(item.published_at is not None for item in items)
    for item in items:
        title = html.escape(item.title)
        if item.url.startswith(("http://", "https://")):
            title = (
                f'<a href="{html.escape(item.url, quote=True)}">{title}</a>'
            )
        elif item.url:
            title = f"{title} ({html.escape(item.url)})"
        published_cell = ""
        if show_published:
            published_cell = (
                f"<td>{html.escape(published_text(item.published_at) or '')}</td>"
            )
        rows.append(
            "<tr>"
            f"<td>{title}</td>"
            f"<td>{html.escape(_price_text(item))}</td>"
            f"{published_cell}"
            f"<td>{html.escape(extra_value(item, 'account'))}</td>"
            f"<td>{html.escape(extra_value(item, 'server'))}</td>"
            f"<td>{html.escape(extra_value(item, 'platform'))}</td>"
            "</tr>"
        )
        details.append(_html_details(item, include_images, include_safe_report))

    published_header = "<th>上架时间</th>" if show_published else ""
    return (
        "<html><body>"
        "<h2>交易猫新品提醒</h2>"
        '<table border="1" cellpadding="4" cellspacing="0">'
        "<thead><tr><th>标题</th><th>价格</th>"
        f"{published_header}"
        "<th>账号</th><th>服务器</th><th>平台</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "".join(details)
        + "</body></html>"
    )


def _html_details(
    item: Listing,
    include_images: bool = True,
    include_safe_report: bool = True,
) -> str:
    """Render the fields that do not fit the summary table."""
    parts: list[str] = [f"<h3>{html.escape(item.title)}</h3>"]

    points = sell_points_text(item)
    if points:
        parts.append(f"<p>特点：{html.escape(points)}</p>")
    note = seller_note(item)
    if note:
        parts.append(f"<p>卖家说：{html.escape(note)}</p>")
    if include_safe_report:
        for line in safe_report_lines(item.safe_report):
            parts.append(f"<p>{html.escape(line)}</p>")
    if include_images and item.images:
        links = " ".join(
            f'<a href="{html.escape(url, quote=True)}">图{index}</a>'
            for index, url in enumerate(item.images, 1)
        )
        parts.append(f"<p>图片：{links}</p>")
    return "".join(parts) if len(parts) > 1 else ""
