"""Domain models shared by adapters, pipeline and notifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class SafeReport:
    """The marketplace's account safety report for one listing.

    ``groups`` holds ready-to-render lines such as
    ``"卖家信用认证：身份证认证 认证通过 / 人脸认证 认证通过"``.  The report is
    only available on the product detail page, so listings fetched from the
    search page carry ``safe_report=None`` until they are enriched.

    ``account_transfer`` is the raw status text of the 「账号转移」step inside
    「交易安全保障」 (``"是"``/``"否"``, ``""`` when the step is absent).  It is
    kept structured because the pipeline filters on it.
    """

    level: str = ""
    percent: str = ""
    note: str = ""
    groups: tuple[str, ...] = ()
    account_transfer: str = ""


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
    images: tuple[str, ...] = ()
    safe_report: SafeReport | None = None
