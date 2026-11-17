import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from monitor.sites.jiaoyimao import parse_listings, parse_published_at, is_punish

FIXTURE = Path(__file__).parent / "fixtures" / "jiaoyimao_list.html"

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
    assert first.url == "https://www.jiaoyimao.com/jg1008016/1793434810016540.html"

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
