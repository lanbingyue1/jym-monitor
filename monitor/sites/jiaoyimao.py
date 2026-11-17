"""Parser for jiaoyimao (交易猫) SSR search result pages.

The search page embeds its state in ``window.__INITIAL_DATA__``.  The JSON is
not strictly valid (it contains bare ``undefined`` values) but is otherwise
well-formed, so it is repaired and decoded with ``json.JSONDecoder``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from monitor.models import Listing

SITE = "jiaoyimao"

_INITIAL_DATA_MARKER = "window.__INITIAL_DATA__"
_PUNISH_SCAN_LIMIT = 6000
_PUNISH_MARKERS = ("x5secdata", "_____tmd_____/punish")
_UNDEFINED_RE = re.compile(r"(?<=:)\s*undefined(?=\s*[,}])")
_EARLIEST_PUBLISHED_AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_DETAIL_URL_TEMPLATE = "https://www.jiaoyimao.com/jg1008016/{goods_id}.html"


def is_punish(html: str) -> bool:
    """Return True when the page is an anti-bot punish page."""
    head = html[:_PUNISH_SCAN_LIMIT]
    return any(marker in head for marker in _PUNISH_MARKERS)


def parse_published_at(item_id: str, now: datetime | None = None) -> datetime | None:
    """Derive the publish time from the snowflake-like goods id.

    The first 13 digits are a millisecond epoch timestamp.  Values outside
    2020-01-01 .. now + 1 day are treated as invalid.
    """
    try:
        published = datetime.fromtimestamp(int(item_id[:13]) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if published < _EARLIEST_PUBLISHED_AT or published > now + timedelta(days=1):
        return None
    return published


def parse_listings(html: str) -> list[Listing]:
    """Parse the goods list from a jiaoyimao SSR search page.

    Returns an empty list for a valid page with no goods and raises
    ``ValueError`` when the page is not a parseable result page.
    """
    goods_list = _extract_goods_list(html)

    listings: list[Listing] = []
    for entry in goods_list:
        if not isinstance(entry, dict) or entry.get("type") != 8:
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or data.get("goodsId") is None:
            raise ValueError("goods entry is missing its data or goodsId")
        listings.append(_to_listing(data))
    return listings


def _extract_goods_list(html: str) -> list:
    marker_at = html.find(_INITIAL_DATA_MARKER)
    if marker_at == -1:
        raise ValueError("window.__INITIAL_DATA__ not found")

    json_start = html.find("{", marker_at + len(_INITIAL_DATA_MARKER))
    if json_start == -1:
        raise ValueError("no JSON object after window.__INITIAL_DATA__")

    raw = _UNDEFINED_RE.sub("null", html[json_start:])
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
    except ValueError as exc:
        raise ValueError("could not decode window.__INITIAL_DATA__") from exc

    try:
        goods_list = payload["pageInitialProps"]["initialData"]["goodsListData"]["goodsList"]
    except (KeyError, TypeError) as exc:
        raise ValueError("unexpected window.__INITIAL_DATA__ structure") from exc

    if not isinstance(goods_list, list):
        raise ValueError("goodsList is not a list")
    return goods_list


def _to_listing(data: dict) -> Listing:
    goods_id = str(data["goodsId"])

    sell_points = data.get("sellPoints") or []
    extra = {
        "server": data.get("serverName") or "",
        "platform": data.get("newPlatformName") or "",
        "description": data.get("description") or "",
        "sell_points": " ".join(
            str(point.get("desc", ""))
            for point in sell_points
            if isinstance(point, dict)
        ),
    }

    return Listing(
        site=SITE,
        item_id=goods_id,
        title=data.get("title") or "",
        price=data.get("price"),
        url=data.get("detailUrl") or _DETAIL_URL_TEMPLATE.format(goods_id=goods_id),
        published_at=parse_published_at(goods_id),
        extra=extra,
    )
