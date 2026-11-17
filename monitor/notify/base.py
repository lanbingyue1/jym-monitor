"""Common notifier protocol shared by delivery channels."""

from __future__ import annotations

from typing import Protocol

from monitor.models import Listing


class Notifier(Protocol):
    """A delivery channel for listing batches and alert messages."""

    name: str

    def send(self, items: list[Listing]) -> None:
        """Deliver one message containing exactly *items*."""

    def send_text(self, text: str) -> None:
        """Deliver a plain-text alert message."""
