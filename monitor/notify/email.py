"""Email notifier built on the standard library ``smtplib``/``email``."""

from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage

from monitor.errors import NotifyError
from monitor.models import Listing
from monitor.notify.base import published_text

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
    ):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.to = _parse_addresses(to)
        self.use_ssl = use_ssl

    def send(self, items: list[Listing]) -> None:
        if not items:
            return
        message = EmailMessage()
        message["Subject"] = f"交易猫新品提醒 ({len(items)})"
        message["From"] = self.user
        message["To"] = ", ".join(self.to)
        message.set_content(_plain_body(items))
        message.add_alternative(_html_body(items), subtype="html")
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


def _extra_text(item: Listing, key: str) -> str:
    value = item.extra.get(key)
    if value is None or value == "":
        return "未知"
    return str(value)


def _plain_body(items: list[Listing]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(item.title)
        lines.append(f"价格：{_price_text(item)}")
        published = published_text(item.published_at)
        if published:
            lines.append(f"上架时间：{published}")
        lines.append(
            f"服务器：{_extra_text(item, 'server')}"
            f" · 平台：{_extra_text(item, 'platform')}"
        )
        lines.append(item.url)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _html_body(items: list[Listing]) -> str:
    rows: list[str] = []
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
            f"<td>{html.escape(_extra_text(item, 'server'))}</td>"
            f"<td>{html.escape(_extra_text(item, 'platform'))}</td>"
            "</tr>"
        )
    published_header = "<th>上架时间</th>" if show_published else ""
    return (
        "<html><body>"
        "<h2>交易猫新品提醒</h2>"
        '<table border="1" cellpadding="4" cellspacing="0">'
        "<thead><tr><th>标题</th><th>价格</th>"
        f"{published_header}"
        "<th>服务器</th><th>平台</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )
