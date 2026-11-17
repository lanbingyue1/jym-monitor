import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from monitor.sites.jiaoyimao import (
    is_punish,
    parse_detail_report,
    parse_listings,
    parse_published_at,
)

FIXTURE = Path(__file__).parent / "fixtures" / "jiaoyimao_list.html"
DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "jiaoyimao_detail.html"


def _html_with_goods(goods_list):
    payload = {
        "pageInitialProps": {
            "initialData": {"goodsListData": {"goodsList": goods_list}}
        }
    }
    return "<script>window.__INITIAL_DATA__=" + json.dumps(payload) + ";</script>"


def test_parse_real_fixture():
    items = parse_listings(FIXTURE.read_text(encoding="utf-8"))
    assert len(items) == 16
    first = items[0]
    assert first.site == "jiaoyimao"
    assert first.item_id == "1793434810016540"
    assert first.price == 180
    assert first.title == "良心出手尽快价格可议"
    assert first.extra["server"] == "玉狐山"
    assert first.extra["platform"] == "安卓"
    assert first.extra["account"] == "联想帐号"
    assert first.url == "https://www.jiaoyimao.com/jg1008016/1793434810016540.html"


def test_parse_real_fixture_includes_images():
    items = parse_listings(FIXTURE.read_text(encoding="utf-8"))
    first = items[0]
    assert len(first.images) == 15
    assert first.images[0].startswith("https://static-cdn.jiaoyimao.com/")
    assert all(url.startswith("https://") for url in first.images)


def test_listing_without_images_has_empty_tuple():
    html = _html_with_goods([{
        "type": 8,
        "data": {"goodsId": 1793434810016540, "title": "t", "price": 1},
    }])
    item = parse_listings(html)[0]
    assert item.images == ()
    assert item.extra["account"] == ""
    assert item.safe_report is None


def test_parse_detail_report_from_real_fixture():
    report = parse_detail_report(DETAIL_FIXTURE.read_text(encoding="utf-8"))
    assert report is not None
    assert report.level == "SSS"
    assert report.percent == "100%"
    assert report.note == "100% 不会被找回"
    assert len(report.groups) == 3
    assert report.groups[0].startswith(
        "卖家信用认证：身份证认证 认证通过 / 人脸认证 认证通过"
    )
    assert "卖家风险校验 校验通过" in report.groups[0]
    assert report.groups[1].startswith("交易安全保障：账号转移 是")
    assert "充值包赔 不支持加购" in report.groups[2]


def test_parse_detail_report_without_report_returns_none():
    payload = {"pageInitialProps": {"initialData": {"goodsData": {
        "sortedComponents": [{"componentKey": "ItemTextIntro"}]
    }}}}
    html = "<script>window.__INITIAL_DATA__=" + json.dumps(payload) + ";</script>"
    assert parse_detail_report(html) is None


def test_parse_detail_report_rejects_search_page():
    with pytest.raises(ValueError):
        parse_detail_report(FIXTURE.read_text(encoding="utf-8"))

def test_published_at_from_goods_id():
    dt = parse_published_at("1794841605849819",
                            now=datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc))
    assert dt == datetime(2026, 11, 16, 15, 6, 45, 849000, tzinfo=timezone.utc)

def test_invalid_goods_id_returns_none():
    assert parse_published_at("abc", now=datetime(2026, 11, 16, tzinfo=timezone.utc)) is None

def test_empty_goods_list_is_valid():
    html = "<script>window.__INITIAL_DATA__=" + json.dumps(
        {"pageInitialProps": {"initialData": {"goodsListData": {"goodsList": []}}}}
    ) + ";</script>"
    assert parse_listings(html) == []

def test_missing_initial_data_raises():
    with pytest.raises(ValueError):
        parse_listings("<html>not the right page</html>")

def test_detect_punish_page():
    assert is_punish('<script>var url=".../_____tmd_____/punish?x5secdata=abc"</script>')
    assert not is_punish("<html>ok</html>")


def test_missing_detail_url_uses_fallback():
    html = _html_with_goods([{
        "type": 8,
        "data": {
            "goodsId": 1793434810016540,
            "title": "t",
            "price": 1,
            "serverName": "s",
            "newPlatformName": "p",
            "description": "d",
            "sellPoints": [],
        },
    }])
    items = parse_listings(html)
    assert len(items) == 1
    assert items[0].url == (
        "https://www.jiaoyimao.com/jg1008016/1793434810016540.html"
    )


def test_malformed_type8_entry_raises():
    html = _html_with_goods([{"type": 8, "data": {"title": "missing goodsId"}}])
    with pytest.raises(ValueError):
        parse_listings(html)


def test_published_at_rejects_too_old_timestamp():
    assert parse_published_at(
        "1000000000000", now=datetime(2026, 11, 16, tzinfo=timezone.utc)
    ) is None


def _price_of(raw):
    html = _html_with_goods([{
        "type": 8,
        "data": {
            "goodsId": 1793434810016540,
            "title": "t",
            "price": raw,
            "sellPoints": [],
        },
    }])
    return parse_listings(html)[0].price


def test_price_integer_is_preserved():
    assert _price_of(180) == 180


def test_price_numeric_string_is_normalized():
    assert _price_of("180") == 180
    assert _price_of("180.0") == 180


def test_price_integral_float_is_normalized():
    price = _price_of(180.0)
    assert price == 180 and type(price) is int


def test_price_non_numeric_is_none():
    assert _price_of("面议") is None
    assert _price_of(None) is None


def test_price_non_integral_number_is_none():
    assert _price_of("180.5") is None
    assert _price_of(180.5) is None
