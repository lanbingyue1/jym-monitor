"""Email notifier built on the standard library ``smtplib``/``email``."""

from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage

from monitor.errors import NotifyError
from monitor.models import Listing

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

    def send_text(self, text: str) -> None:
        message = EmailMessage()
        message["Subject"] = ALERT_SUBJECT
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
            recipients = ", ".join(self.to)
            raise NotifyError(
                f"email delivery to {recipients} failed: {exc}"
            ) from exc


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
        lines.append(
            f"服务器：{_extra_text(item, 'server')}"
            f" · 平台：{_extra_text(item, 'platform')}"
        )
        lines.append(item.url)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _html_body(items: list[Listing]) -> str:
    rows: list[str] = []
    for item in items:
        title = html.escape(item.title)
        if item.url.startswith(("http://", "https://")):
            title = (
                f'<a href="{html.escape(item.url, quote=True)}">{title}</a>'
            )
        elif item.url:
            title = f"{title} ({html.escape(item.url)})"
        rows.append(
            "<tr>"
            f"<td>{title}</td>"
            f"<td>{html.escape(_price_text(item))}</td>"
            f"<td>{html.escape(_extra_text(item, 'server'))}</td>"
            f"<td>{html.escape(_extra_text(item, 'platform'))}</td>"
            "</tr>"
        )
    return (
        "<html><body>"
        "<h2>交易猫新品提醒</h2>"
        '<table border="1" cellpadding="4" cellspacing="0">'
        "<thead><tr><th>标题</th><th>价格</th><th>服务器</th><th>平台</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )
