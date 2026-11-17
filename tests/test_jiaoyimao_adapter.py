"""Tests for the jiaoyimao adapter: pagination, dedup and browser fallback."""

from pathlib import Path

import pytest

from monitor.config import Config, Filters, SiteConfig
from monitor.errors import ConfigError, FetchError
from monitor.sites import build_adapters
from monitor.sites import jiaoyimao
from monitor.sites.jiaoyimao import JiaoyimaoAdapter

FIXTURE_HTML = (
    Path(__file__).parent / "fixtures" / "jiaoyimao_list.html"
).read_text(encoding="utf-8")
KNOWN_ID = "1793434810016540"
BASE_URL = "https://example.com/list"
ENCODED_URL = (
    "https://www.jiaoyimao.com/jg1008016/f2161571-c2161572/o110/"
    "?rId=107&searchCondition=%7B%22filter_low_quality%22%3A%7B"
    "%22conditionList%22%3A%5B%221%22%5D%7D%7D&enforcePlat=2&newPage=true"
)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(jiaoyimao.time, "sleep", recorded.append)
    return recorded


def make_adapter(htmls, calls, urls=(BASE_URL,), max_pages=3):
    def http_fetch(url):
        calls.append(url)
        return htmls[min(len(calls) - 1, len(htmls) - 1)]

    return JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=urls,
            max_pages=max_pages,
            filters=Filters(),
        ),
        http_fetch=http_fetch,
    )


def test_stops_paging_when_known_id_seen():
    calls = []
    a = make_adapter([FIXTURE_HTML], calls)
    items = a.fetch(known_ids=frozenset({KNOWN_ID}))
    assert len(calls) == 1
    assert len(items) == 16
    assert a.saturated is False


def test_pages_until_max_when_all_new():
    calls = []
    a = make_adapter([FIXTURE_HTML, FIXTURE_HTML, FIXTURE_HTML], calls)
    items = a.fetch(known_ids=frozenset())
    assert len(calls) == 3 and "page=3" in calls[-1]
    assert len(items) == 16  # 同一页重复抓取，按 item_id 去重
    assert a.saturated is True


def test_falls_back_to_browser_on_fetch_error():
    from monitor.errors import FetchError

    def bad_fetch(url):
        raise FetchError("punish")

    calls = []
    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=("https://example.com/list",),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=bad_fetch,
        browser_fetch=lambda url: calls.append(url) or FIXTURE_HTML,
    )
    items = a.fetch()
    assert len(items) == 16 and len(calls) == 1 and a.used_browser is True


def test_first_page_url_contains_explicit_page_and_preserves_query():
    calls = []
    a = make_adapter([FIXTURE_HTML], calls, urls=(ENCODED_URL,), max_pages=2)
    a.fetch(known_ids=frozenset({KNOWN_ID}))
    assert calls == [
        ENCODED_URL
        + "&page=1"
    ]


def test_page_parameter_is_added_at_end_and_incremented():
    calls = []
    a = make_adapter([FIXTURE_HTML], calls, max_pages=2)
    a.fetch(known_ids=frozenset())
    assert calls == [BASE_URL + "?page=1", BASE_URL + "?page=2"]


def test_replaces_existing_page_parameter():
    calls = []
    a = make_adapter(
        [FIXTURE_HTML], calls, urls=(BASE_URL + "?rId=107&page=9",), max_pages=1
    )
    a.fetch(known_ids=frozenset({KNOWN_ID}))
    assert calls == [BASE_URL + "?rId=107&page=1"]


def test_sleeps_only_between_pages_of_the_same_url(sleeps):
    calls = []
    a = make_adapter(
        [FIXTURE_HTML], calls, urls=(BASE_URL, "https://example.com/other"),
        max_pages=2,
    )
    a.fetch(known_ids=frozenset())
    assert len(calls) == 4
    assert sleeps == [1.5, 1.5]


def test_parse_failure_is_fetch_error_and_does_not_use_browser():
    browser_calls = []

    def http_fetch(url):
        # The SSR marker is present, so this is a parse failure rather than an
        # anti-bot JS shell: the browser must not be involved.
        return "<script>window.__INITIAL_DATA__ = {broken json}</script>"

    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=(BASE_URL,),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=http_fetch,
        browser_fetch=lambda url: browser_calls.append(url) or FIXTURE_HTML,
    )
    with pytest.raises(FetchError):
        a.fetch()
    assert browser_calls == []


def test_missing_initial_data_falls_back_to_browser_once():
    browser_calls = []

    def http_fetch(url):
        # A 200 response that is only the JS shell: valid HTTP, no SSR data.
        return "<html><body><div id='app'></div></body></html>"

    def browser_fetch(url):
        browser_calls.append(url)
        return FIXTURE_HTML

    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=(BASE_URL,),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=http_fetch,
        browser_fetch=browser_fetch,
    )
    items = a.fetch()

    assert len(items) == 16
    assert a.used_browser is True
    assert browser_calls == [BASE_URL + "?page=1"]


def test_missing_initial_data_in_http_and_browser_is_fetch_error():
    browser_calls = []

    def http_fetch(url):
        return "<html><body>shell</body></html>"

    def browser_fetch(url):
        browser_calls.append(url)
        return "<html><body>still a shell</body></html>"

    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=(BASE_URL,),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=http_fetch,
        browser_fetch=browser_fetch,
    )
    with pytest.raises(FetchError, match="page=1") as excinfo:
        a.fetch()

    assert browser_calls == [BASE_URL + "?page=1"]
    assert a.used_browser is True
    assert "INITIAL_DATA" in str(excinfo.value)


def test_browser_failure_propagates():
    def bad_fetch(url):
        raise FetchError("http blocked")

    def bad_browser(url):
        raise FetchError("browser blocked")

    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=(BASE_URL,),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=bad_fetch,
        browser_fetch=bad_browser,
    )
    with pytest.raises(FetchError, match="browser blocked"):
        a.fetch()
    assert a.used_browser is True


def test_flags_reset_at_the_start_of_each_fetch():
    http_calls = []

    def http_fetch(url):
        http_calls.append(url)
        if len(http_calls) == 1:
            raise FetchError("punish")
        return FIXTURE_HTML

    a = JiaoyimaoAdapter(
        SiteConfig(
            site_id="jiaoyimao",
            enabled=True,
            urls=(BASE_URL,),
            max_pages=1,
            filters=Filters(),
        ),
        http_fetch=http_fetch,
        browser_fetch=lambda url: FIXTURE_HTML,
    )
    a.fetch(known_ids=frozenset())
    assert a.used_browser is True and a.saturated is True
    a.fetch(known_ids=frozenset({KNOWN_ID}))
    assert a.used_browser is False and a.saturated is False


def test_build_adapters_constructs_enabled_jiaoyimao():
    cfg = Config(
        sites={
            "jiaoyimao": SiteConfig(
                site_id="jiaoyimao",
                enabled=True,
                urls=(BASE_URL,),
                max_pages=1,
                filters=Filters(),
            )
        }
    )
    adapters = build_adapters(cfg)
    assert len(adapters) == 1
    assert isinstance(adapters[0], JiaoyimaoAdapter)
    assert adapters[0].site_id == "jiaoyimao"


def test_build_adapters_skips_disabled_site():
    cfg = Config(
        sites={
            "jiaoyimao": SiteConfig(
                site_id="jiaoyimao",
                enabled=False,
                urls=(BASE_URL,),
                max_pages=1,
                filters=Filters(),
            )
        }
    )
    assert build_adapters(cfg) == []


def test_build_adapters_rejects_unknown_site():
    cfg = Config(
        sites={
            "unknown": SiteConfig(
                site_id="unknown",
                enabled=True,
                urls=(BASE_URL,),
                max_pages=1,
                filters=Filters(),
            )
        }
    )
    with pytest.raises(ConfigError):
        build_adapters(cfg)


def test_build_adapters_rejects_unknown_site_even_when_disabled():
    cfg = Config(
        sites={
            "unknown": SiteConfig(
                site_id="unknown",
                enabled=False,
                urls=(BASE_URL,),
                max_pages=1,
                filters=Filters(),
            )
        }
    )
    with pytest.raises(ConfigError):
        build_adapters(cfg)
