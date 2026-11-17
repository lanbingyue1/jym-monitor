"""Parser for jiaoyimao (交易猫) SSR search result pages.

The search page embeds its state in ``window.__INITIAL_DATA__``.  The JSON is
not strictly valid (it contains bare ``undefined`` values) but is otherwise
well-formed, so it is repaired and decoded with ``json.JSONDecoder``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from monitor.errors import FetchError
from monitor.models import Listing

SITE = "jiaoyimao"

_INITIAL_DATA_MARKER = "window.__INITIAL_DATA__"
_PUNISH_SCAN_LIMIT = 6000
_PUNISH_MARKERS = ("x5secdata", "_____tmd_____/punish")
_UNDEFINED_RE = re.compile(r"(?<=:)\s*undefined(?=\s*[,}])")
_EARLIEST_PUBLISHED_AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_DETAIL_URL_TEMPLATE = "https://www.jiaoyimao.com/jg1008016/{goods_id}.html"

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.jiaoyimao.com/",
}
_FETCH_TIMEOUT_SECONDS = 30
_RETRY_WAIT_SECONDS = 3
_BROWSER_WAIT_SECONDS = 6
_BROWSER_TIMEOUT_MILLIS = 90_000
_WEBDRIVER_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)


def is_punish(html: str) -> bool:
    """Return True when the page is an anti-bot punish page."""
    head = html[:_PUNISH_SCAN_LIMIT]
    return any(marker in head for marker in _PUNISH_MARKERS)


def fetch_html(url: str, session: requests.Session | None = None) -> str:
    """Fetch *url* over HTTP, retrying once after a punish or non-2xx reply."""
    get = session.get if session is not None else requests.get
    last_exc: Exception | None = None

    for attempt in range(2):
        if attempt:
            time.sleep(_RETRY_WAIT_SECONDS)
        try:
            response = get(url, headers=_FETCH_HEADERS, timeout=_FETCH_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            continue
        if 200 <= response.status_code < 300 and not is_punish(response.text):
            return response.text
        last_exc = None

    raise FetchError(f"could not fetch {url}") from last_exc


def _ensure_browser_response_ok(response, url: str) -> None:
    """Raise ``FetchError`` when a browser navigation did not return 2xx."""
    if response is None or not response.ok:
        status = "none" if response is None else response.status
        raise FetchError(f"browser navigation failed for {url} (HTTP status {status})")


def fetch_with_browser(url: str) -> str:
    """Fetch *url* with a real Chrome via Playwright as an anti-bot fallback."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = browser.new_page()
                page.add_init_script(_WEBDRIVER_INIT_SCRIPT)
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=_BROWSER_TIMEOUT_MILLIS,
                )
                _ensure_browser_response_ok(response, url)
                time.sleep(_BROWSER_WAIT_SECONDS)
                html = page.content()
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"browser fetch failed for {url}") from exc

    if is_punish(html):
        raise FetchError(f"browser fetch still hit the anti-bot page for {url}")
    return html


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
