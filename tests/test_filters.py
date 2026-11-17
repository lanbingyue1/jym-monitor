from monitor.filters import matches
from monitor.config import Filters
from monitor.models import Listing


def L(**extra):
    return Listing("s", "1", "标题 血影", 100, "u", None, extra)


def test_empty_filters_pass_everything():
    assert matches(L(platform="安卓", server="和之国"), Filters())


def test_price_and_platform_and_keyword():
    f = Filters(price_min=50, price_max=200, platforms=("安卓",), include_keywords=("血影",))
    assert matches(L(platform="安卓", description="有血影皮肤"), f)
    assert not matches(Listing("s", "2", "x", 999, "u", None, {"platform": "安卓"}), f)
    assert not matches(L(platform="iOS"), f)


def test_exclude_keyword():
    f = Filters(exclude_keywords=("求购",))
    assert not matches(Listing("s", "3", "求购账号", 1, "u", None, {}), f)


def test_price_none_fails_only_when_a_bound_is_set():
    unknown = Listing("s", "4", "x", None, "u", None, {})
    assert matches(unknown, Filters())
    assert not matches(unknown, Filters(price_min=1))
    assert not matches(unknown, Filters(price_max=1))


def test_price_bounds_are_inclusive():
    assert matches(Listing("s", "5", "x", 50, "u", None, {}), Filters(price_min=50))
    assert matches(Listing("s", "6", "x", 200, "u", None, {}), Filters(price_max=200))
    assert not matches(Listing("s", "7", "x", 49, "u", None, {}), Filters(price_min=50))
    assert not matches(Listing("s", "8", "x", 201, "u", None, {}), Filters(price_max=200))


def test_server_filter_is_exact_and_requires_the_field():
    f = Filters(servers=("和之国",))
    assert matches(L(server="和之国"), f)
    assert not matches(L(server="玉狐山"), f)
    assert not matches(L(), f)


def test_platform_filter_is_exact_and_requires_the_field():
    f = Filters(platforms=("安卓",))
    assert matches(L(platform="安卓"), f)
    assert not matches(L(platform="iOS"), f)
    assert not matches(L(), f)


def test_include_keywords_are_case_insensitive_any_match():
    f = Filters(include_keywords=("BLOOD", "血影"))
    assert matches(L(description="has blood skin"), f)
    assert matches(Listing("s", "1", "t", 1, "u", None, {"sell_points": "稀有 血影"}), f)
    assert not matches(Listing("s", "2", "t", 1, "u", None, {}), f)


def test_include_keyword_searches_title_description_and_sell_points():
    assert matches(L(), Filters(include_keywords=("标题",)))
    assert matches(L(description="有血影皮肤"), Filters(include_keywords=("皮肤",)))
    assert matches(
        Listing("s", "1", "t", 1, "u", None, {"sell_points": "满命"}),
        Filters(include_keywords=("满命",)),
    )


def test_exclude_keyword_wins_over_include():
    f = Filters(include_keywords=("血影",), exclude_keywords=("求购",))
    assert not matches(Listing("s", "3", "求购 血影", 1, "u", None, {}), f)
