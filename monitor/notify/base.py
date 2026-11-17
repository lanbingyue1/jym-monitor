"""Common notifier protocol shared by delivery channels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from monitor.models import Listing

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


class Notifier(Protocol):
    """A delivery channel for listing batches and alert messages."""

    name: str

    def send(self, items: list[Listing]) -> None:
        """Deliver one message containing exactly *items*."""

    def send_text(self, text: str, *, subject: str | None = None) -> None:
        """Deliver a plain-text message, optionally under *subject*."""
