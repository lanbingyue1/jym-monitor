"""Domain models shared by adapters, pipeline and notifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class Listing:
    """A normalized marketplace listing."""

    site: str
    item_id: str
    title: str
    price: int | None
    url: str
    published_at: datetime | None
    extra: Mapping[str, str] = field(default_factory=dict)
