"""Email notifier built on the standard library ``smtplib``/``email``."""

from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit

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
# Mail is usually opened on a phone; the desktop pages render poorly there.
_MOBILE_HOSTS = {"www.jiaoyimao.com": "m.jiaoyimao.com"}


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
    return "价格未知" if item.price is None else f"¥{item.price:,}"


def _mobile_url(url: str) -> str:
    """Rewrite a marketplace URL to its mobile host, keeping path and query."""
    parts = urlsplit(url)
    mobile = _MOBILE_HOSTS.get(parts.netloc.lower())
    if mobile is None:
        return url
    return urlunsplit(parts._replace(netloc=mobile))


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
        lines.append(_mobile_url(item.url))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Email clients ignore webfonts and most CSS resets; system fonts and inline
# styles are the only things that render consistently on phones.
_FONT = (
    "-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',"
    "'Microsoft YaHei',sans-serif"
)


def _html_body(
    items: list[Listing],
    include_images: bool = True,
    include_safe_report: bool = True,
) -> str:
    """Render a mobile-first HTML mail: one card per listing."""
    cards = "".join(
        _html_card(item, include_images, include_safe_report) for item in items
    )
    preheader = f"{len(items)} 件新商品：{items[0].title}"
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        "<title>交易猫新品提醒</title>"
        "<style>@media only screen and (max-width:600px){"
        ".wrap{padding:10px !important}"
        ".card{padding:14px !important}"
        ".title{font-size:17px !important}"
        "}</style></head>"
        f'<body style="margin:0;padding:0;background:#f2f3f5;font-family:{_FONT};">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'color:transparent;">{html.escape(preheader)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="background:#f2f3f5;">'
        '<tr><td align="center" style="padding:16px 10px;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="max-width:600px;">'
        '<tr><td style="padding:2px 4px 12px 4px;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0"><tr>'
        '<td style="font-size:18px;font-weight:700;color:#1f2329;">'
        "交易猫新品提醒</td>"
        '<td align="right" style="font-size:13px;color:#8f959e;'
        f'white-space:nowrap;">{len(items)} 件</td>'
        "</tr></table></td></tr>"
        f"{cards}"
        '<tr><td style="padding:6px 4px 0 4px;font-size:12px;'
        'line-height:1.6;color:#a0a6ad;">'
        "忍者必须死3 · 成品号 · 由 jym-monitor 自动发送"
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def _html_card(
    item: Listing,
    include_images: bool,
    include_safe_report: bool,
) -> str:
    """Render one listing as a self-contained card row pair."""
    parts: list[str] = []
    link = _mobile_url(item.url)
    published = published_text(item.published_at)
    time_cell = (
        '<td style="font-size:12px;color:#8f959e;vertical-align:middle;">'
        f"上架时间 {html.escape(published)}</td>"
        if published
        else "<td></td>"
    )
    parts.append(
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0"><tr>'
        f"{time_cell}"
        '<td align="right" style="font-size:22px;line-height:1.1;'
        'font-weight:700;color:#e4393c;white-space:nowrap;">'
        f"{html.escape(_price_text(item))}</td>"
        "</tr></table>"
    )

    title = html.escape(item.title)
    if _is_http(link):
        title = (
            f'<a href="{html.escape(link, quote=True)}" '
            f'style="color:#1f2329;text-decoration:none;">{title}</a>'
        )
    parts.append(
        '<div class="title" style="margin:8px 0 12px 0;font-size:16px;'
        'line-height:1.45;font-weight:600;color:#1f2329;'
        f'word-break:break-word;">{title}</div>'
    )

    parts.append(_html_attributes(item))

    points = sell_points_text(item)
    if points:
        parts.append(
            '<div style="margin:12px 0 0 0;font-size:14px;line-height:1.5;'
            'color:#1f2329;">'
            '<span style="color:#8f959e;">特点：</span>'
            f"{html.escape(points)}</div>"
        )
    note = seller_note(item)
    if note:
        parts.append(
            '<div style="margin:10px 0 0 0;padding:10px 12px;'
            'background:#fff7e8;border-radius:8px;font-size:14px;'
            'line-height:1.55;color:#8a5a00;">'
            '<span style="color:#b26a00;font-weight:600;">卖家说：</span>'
            f"{html.escape(note)}</div>"
        )
    if include_safe_report:
        report = safe_report_lines(item.safe_report)
        if report:
            body = "<br>".join(html.escape(line) for line in report)
            parts.append(
                '<div style="margin:10px 0 0 0;padding:10px 12px;'
                'background:#eef7ee;border-radius:8px;font-size:13px;'
                f'line-height:1.6;color:#2f6b2f;">{body}</div>'
            )
    if include_images and item.images:
        links = " ".join(
            f'<a href="{html.escape(url, quote=True)}" '
            f'style="color:#1677ff;text-decoration:none;">图{index}</a>'
            for index, url in enumerate(item.images, 1)
        )
        parts.append(
            '<div style="margin:10px 0 0 0;font-size:13px;color:#646a73;">'
            f"图片：{links}</div>"
        )

    if _is_http(link):
        parts.append(
            '<table role="presentation" width="100%" cellpadding="0" '
            'cellspacing="0" style="margin:14px 0 0 0;"><tr>'
            '<td align="center" style="background:#1677ff;'
            'border-radius:8px;">'
            f'<a href="{html.escape(link, quote=True)}" '
            'style="display:block;padding:12px 16px;font-size:15px;'
            'font-weight:600;color:#ffffff;text-decoration:none;">'
            "查看商品详情 →</a></td></tr></table>"
        )
    elif item.url:
        parts.append(
            '<div style="margin:10px 0 0 0;font-size:12px;color:#8f959e;'
            f'word-break:break-all;">{html.escape(link)}</div>'
        )

    return (
        '<tr><td class="card" style="padding:16px;background:#ffffff;'
        'border:1px solid #e5e6eb;border-radius:12px;">'
        + "".join(parts)
        + "</td></tr>"
        '<tr><td style="height:12px;line-height:12px;font-size:0;">'
        "&nbsp;</td></tr>"
    )


def _html_attributes(item: Listing) -> str:
    """Render 账号 / 平台 / 服务器 as label-value rows, readable on a phone."""
    rows = (
        ("账号", extra_value(item, "account")),
        ("平台", extra_value(item, "platform")),
        ("服务器", extra_value(item, "server")),
    )
    cells: list[str] = []
    last = len(rows) - 1
    for index, (label, value) in enumerate(rows):
        if index == 0:
            padding = "10px 12px 4px 12px"
        elif index == last:
            padding = "4px 12px 10px 12px"
        else:
            padding = "4px 12px"
        cells.append(
            f'<tr><td style="padding:{padding};font-size:12px;color:#8f959e;'
            f'width:56px;vertical-align:top;">{label}</td>'
            f'<td style="padding:{padding};font-size:14px;color:#1f2329;">'
            f"{html.escape(value)}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="background:#f7f8fa;border-radius:8px;">'
        + "".join(cells)
        + "</table>"
    )


def _is_http(url: str) -> bool:
    return url.startswith(("http://", "https://"))
