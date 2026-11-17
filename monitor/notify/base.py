"""Common notifier protocol shared by delivery channels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from monitor.models import Listing, SafeReport

_BEIJING = timezone(timedelta(hours=8))


def published_text(published_at: datetime | None) -> str | None:
    """Render *published_at* as Beijing time ``YYYY-MM-DD HH:MM``.

    Returns ``None`` when the publication time is unknown.  Naive datetimes are
    interpreted as UTC so the output never depends on the runner's local zone.
    """
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M")


def extra_value(item: Listing, key: str) -> str:
    """Return ``item.extra[key]`` or ``"未知"`` when it is missing."""
    value = item.extra.get(key)
    if value is None or value == "":
        return "未知"
    return str(value)


def seller_note(item: Listing) -> str | None:
    """Return the seller's own description (卖家说), if any."""
    value = item.extra.get("description")
    text = str(value).strip() if value is not None else ""
    return text or None


def sell_points_text(item: Listing) -> str | None:
    """Return the listing's highlighted sell points (e.g. 忍阶/角色), if any."""
    value = item.extra.get("sell_points")
    text = str(value).strip() if value is not None else ""
    return text or None


def safe_report_lines(report: SafeReport | None) -> list[str]:
    """Render the safety report as one headline plus one line per group."""
    if report is None:
        return []

    headline = "安全报告：" + (report.level or "未知等级")
    if report.percent:
        headline += f" {report.percent}"
    if report.note:
        headline += f"（{report.note}）"
    return [headline, *report.groups]


class Notifier(Protocol):
    """A delivery channel for listing batches and alert messages."""

    name: str

    def send(self, items: list[Listing]) -> None:
        """Deliver one message containing exactly *items*."""

    def send_text(self, text: str, *, subject: str | None = None) -> None:
        """Deliver a plain-text message, optionally under *subject*."""
