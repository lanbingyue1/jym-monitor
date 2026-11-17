"""Parser for jiaoyimao (交易猫) SSR search result pages.

The search page embeds its state in ``window.__INITIAL_DATA__``.  The JSON is
not strictly valid (it contains bare ``undefined`` values) but is otherwise
well-formed, so it is repaired and decoded with ``json.JSONDecoder``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Callable
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import requests

from monitor.config import SiteConfig
from monitor.errors import FetchError
from monitor.models import Listing, SafeReport

SITE = "jiaoyimao"
PAGE_SIZE = 16
PAGE_SLEEP_SECONDS = 1.5
DETAIL_SLEEP_SECONDS = 1.0

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


def _decode_initial_data(html: str) -> dict:
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
    if not isinstance(payload, dict):
        raise ValueError("unexpected window.__INITIAL_DATA__ structure")
    return payload


def _extract_goods_list(html: str) -> list:
    payload = _decode_initial_data(html)

    try:
        goods_list = payload["pageInitialProps"]["initialData"]["goodsListData"]["goodsList"]
    except (KeyError, TypeError) as exc:
        raise ValueError("unexpected window.__INITIAL_DATA__ structure") from exc

    if not isinstance(goods_list, list):
        raise ValueError("goodsList is not a list")
    return goods_list


def parse_detail_report(html: str) -> SafeReport | None:
    """Return the 安全报告 from a jiaoyimao product detail page.

    Returns ``None`` when the page is parseable but has no safety report.
    Raises ``ValueError`` when the page is not a detail page.
    """
    payload = _decode_initial_data(html)

    try:
        components = payload["pageInitialProps"]["initialData"]["goodsData"][
            "sortedComponents"
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError("unexpected window.__INITIAL_DATA__ structure") from exc
    if not isinstance(components, list):
        raise ValueError("sortedComponents is not a list")

    for component in components:
        if not isinstance(component, dict):
            continue
        biz = component.get("itemPageBizViewData")
        if not isinstance(biz, dict):
            continue
        report = _to_safe_report(biz.get("safeReport"))
        if report is not None:
            return report
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(raw: object) -> str:
    """Strip the inline markup the marketplace embeds in label strings."""
    return unescape(_TAG_RE.sub("", str(raw or ""))).strip()


def _to_safe_report(raw: object) -> SafeReport | None:
    if not isinstance(raw, dict):
        return None

    level_raw = raw.get("safeLevel")
    level_raw = level_raw if isinstance(level_raw, dict) else {}
    content_raw = raw.get("safeContent")
    content_raw = content_raw if isinstance(content_raw, dict) else {}

    groups: list[str] = []
    account_transfer = ""
    content_list = content_raw.get("contentList")
    for group in content_list if isinstance(content_list, list) else []:
        if not isinstance(group, dict):
            continue
        steps = []
        for step in group.get("data") or []:
            if not isinstance(step, dict):
                continue
            text = str(step.get("text") or "").strip()
            status = str(step.get("statusText") or "").strip()
            if text == "账号转移" and status:
                account_transfer = status
            if text and status:
                steps.append(f"{text} {status}")
            elif text:
                steps.append(text)
        if not steps:
            continue
        title = str(group.get("title") or "").strip()
        line = " / ".join(steps)
        groups.append(f"{title}：{line}" if title else line)

    report = SafeReport(
        level=str(level_raw.get("level") or "").strip(),
        percent=str(level_raw.get("levelPercentText") or "").strip(),
        note=_plain_text(level_raw.get("descHtml")),
        groups=tuple(groups),
        account_transfer=account_transfer,
    )
    if not (report.level or report.percent or report.note or report.groups):
        return None
    return report


def _normalize_price(value: object) -> int | None:
    """Return *value* as an integer price, or ``None`` when unknown.

    The marketplace intermittently serializes prices as integers, integral
    floats or numeric strings.  Anything else is treated as an unknown price.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _image_urls(data: dict) -> tuple[str, ...]:
    images = data.get("images")
    if not isinstance(images, list):
        return ()
    urls: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("originImage") or image.get("auditImage")
        if url:
            urls.append(str(url))
    return tuple(urls)


def _to_listing(data: dict) -> Listing:
    goods_id = str(data["goodsId"])

    sell_points = data.get("sellPoints") or []
    category = data.get("category")
    extra = {
        "server": data.get("serverName") or "",
        "platform": data.get("newPlatformName") or "",
        "account": (
            str(category.get("categoryName") or "")
            if isinstance(category, dict)
            else ""
        ),
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
        price=_normalize_price(data.get("price")),
        url=data.get("detailUrl") or _DETAIL_URL_TEMPLATE.format(goods_id=goods_id),
        published_at=parse_published_at(goods_id),
        extra=extra,
        images=_image_urls(data),
    )


def _page_url(url: str, page: int) -> str:
    """Return *url* with its ``page`` query parameter set to *page*.

    Other query parameters are copied verbatim so percent-encoded values such
    as ``searchCondition`` keep their original form.
    """
    parts = urlsplit(url)
    segments = [
        segment
        for segment in parts.query.split("&")
        if segment and unquote_plus(segment.split("=", 1)[0]) != "page"
    ]
    segments.append(f"page={page}")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(segments), parts.fragment)
    )


class JiaoyimaoAdapter:
    """Fetch jiaoyimao search results page by page until known IDs appear."""

    site_id = SITE

    def __init__(
        self,
        cfg: SiteConfig,
        http_fetch: Callable[[str], str] = fetch_html,
        browser_fetch: Callable[[str], str] = fetch_with_browser,
    ) -> None:
        self.cfg = cfg
        self.http_fetch = http_fetch
        self.browser_fetch = browser_fetch
        self.used_browser = False
        self.saturated = False

    def fetch(self, known_ids: frozenset[str] = frozenset()) -> list[Listing]:
        """Return deduplicated listings from every configured URL."""
        self.used_browser = False
        self.saturated = False

        listings: list[Listing] = []
        seen: set[str] = set()
        for url in self.cfg.urls:
            self._fetch_url(url, known_ids, listings, seen)
        return listings

    def _fetch_url(
        self,
        url: str,
        known_ids: frozenset[str],
        listings: list[Listing],
        seen: set[str],
    ) -> None:
        for page in range(1, self.cfg.max_pages + 1):
            if page > 1:
                time.sleep(PAGE_SLEEP_SECONDS)
            page_url = _page_url(url, page)
            html = self._fetch_page(page_url)
            try:
                page_items = parse_listings(html)
            except ValueError as exc:
                raise FetchError(
                    f"could not parse listings from {page_url}: {exc}"
                ) from exc

            for item in page_items:
                if item.item_id not in seen:
                    seen.add(item.item_id)
                    listings.append(item)

            if any(item.item_id in known_ids for item in page_items):
                return

            if page == self.cfg.max_pages and len(page_items) == PAGE_SIZE:
                self.saturated = True

    def fetch_detail(self, item: Listing) -> Listing:
        """Return *item* enriched with the detail page's 安全报告.

        Raises ``FetchError`` when the detail page cannot be fetched or
        parsed; the caller decides whether to keep the unenriched listing.
        """
        time.sleep(DETAIL_SLEEP_SECONDS)
        html = self._fetch_page(item.url)
        try:
            report = parse_detail_report(html)
        except ValueError as exc:
            raise FetchError(
                f"could not parse detail page {item.url}: {exc}"
            ) from exc
        if report is None:
            return item
        return replace(item, safe_report=report)

    def _fetch_page(self, url: str) -> str:
        try:
            html = self.http_fetch(url)
        except FetchError:
            # HTTP was blocked: the existing anti-bot fallback.
            return self._fetch_with_browser_once(url)

        if _INITIAL_DATA_MARKER in html:
            return html

        # HTTP returned 200 but only the JS shell: per the anti-bot risk
        # definition a missing SSR marker is also a risk signal, so try Chrome
        # exactly once before giving up.
        return self._fetch_with_browser_once(url)

    def _fetch_with_browser_once(self, url: str) -> str:
        """Fetch *url* through the browser fallback at most once.

        Shared by both triggers (HTTP failure and a 200 page without SSR data)
        so ``used_browser`` and the single-attempt rule cannot diverge.
        """
        self.used_browser = True
        html = self.browser_fetch(url)
        if _INITIAL_DATA_MARKER not in html:
            raise FetchError(
                f"could not fetch {url}: "
                "window.__INITIAL_DATA__ not found in browser response"
            )
        return html
