import copy
from datetime import datetime, timezone, timedelta

import pytest

from monitor.config import (Config, SiteConfig, Filters, NotifyConfig, AlertConfig)
from monitor.models import Listing
from monitor.pipeline import run_once
from monitor.state import new_state, FileStateStore, ensure_site
from monitor.errors import FetchError, NotifyError, StateError

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def listing(i, published=NOW):
    return Listing("jiaoyimao", i, f"商品{i}", 100, f"https://x/{i}", published,
                   {"server": "和之国", "platform": "安卓"})


class FakeAdapter:
    site_id = "jiaoyimao"
    def __init__(self, items): self.items, self.used_browser, self.saturated = items, False, False
    def fetch(self, known_ids=frozenset()): return self.items


class FailingAdapter:
    site_id = "jiaoyimao"
    used_browser = False
    saturated = False
    def fetch(self, known_ids=frozenset()): raise FetchError("blocked")


class FakeNotifier:
    def __init__(self, name="feishu", fail=False):
        self.name, self.fail, self.items, self.texts = name, fail, [], []
        self.text_subjects = []
    def send(self, items):
        if self.fail: raise NotifyError("x")
        self.items.extend(items)
    def send_text(self, text, *, subject=None):
        if self.fail: raise NotifyError("x")
        self.texts.append(text)
        self.text_subjects.append(subject)


class BrokenStore:
    def load(self): return new_state()
    def save(self, state): raise StateError("disk")


def cfg(bootstrap=True, batch_size=10, threshold=3, interval=180, filters=None):
    return Config(sites={"jiaoyimao": SiteConfig(
                      "jiaoyimao", True, ("u",), 1,
                      Filters() if filters is None else filters)},
                  notify=NotifyConfig(("feishu",), batch_size),
                  alerts=AlertConfig(threshold, interval), bootstrap_notify=bootstrap)


# ---------------------------------------------------------------------------
# Brief tests
# ---------------------------------------------------------------------------

def test_bootstrap_registers_without_item_push(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    assert r.bootstrap is True and n.items == [] and len(n.texts) == 1
    assert "1" in store.load()["sites"]["jiaoyimao"]["seen"]


def test_bootstrap_text_uses_start_subject(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    assert n.text_subjects == ["交易猫监控已启动"]


def test_failure_alert_uses_default_alert_subject(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    c = cfg(threshold=1)
    run_once(c, [FailingAdapter()], [n], store, now=NOW)
    assert n.text_subjects == [None]


def test_bootstrap_silent_when_configured(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(bootstrap=False), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    assert n.texts == [] and "1" in store.load()["sites"]["jiaoyimao"]["seen"]


def test_new_item_notified_and_seen(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    r = run_once(cfg(), [FakeAdapter([listing("1"), listing("2")])], [n], store,
                 now=NOW + timedelta(minutes=5))
    assert r.new == 1 and [x.item_id for x in n.items] == ["2"]
    assert "2" in store.load()["sites"]["jiaoyimao"]["seen"]


def test_old_listing_never_notified(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    old = listing("999", published=NOW - timedelta(days=30))
    r = run_once(cfg(), [FakeAdapter([old])], [n], store, now=NOW + timedelta(minutes=5))
    assert r.new == 0 and n.items == []


def test_notify_failure_does_not_mark_seen(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    run_once(cfg(), [FakeAdapter([listing("1")])], [FakeNotifier()], store, now=NOW)
    bad = FakeNotifier(fail=True)
    r = run_once(cfg(), [FakeAdapter([listing("1"), listing("2")])], [bad], store,
                 now=NOW + timedelta(minutes=5))
    assert r.notified == 0 and r.errors
    assert "2" not in store.load()["sites"]["jiaoyimao"]["seen"]


def test_dry_run_sends_nothing(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    r = run_once(cfg(), [FakeAdapter([listing("1"), listing("2")])], [n], store,
                 now=NOW + timedelta(minutes=5), dry_run=True)
    assert r.new == 1 and n.items == []
    assert len(store.load()["sites"]["jiaoyimao"]["seen"]) == 1


def test_repeated_fetch_failures_alert(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    for i in range(3):
        r = run_once(cfg(), [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=5 * i))
    assert any("[监控异常]" in t for t in n.texts) and r.errors


def test_channel_failure_alerted_via_other_channel(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    bad, good = FakeNotifier(name="feishu", fail=True), FakeNotifier(name="email")
    for i, items in enumerate((["1"], ["1", "2"], ["1", "2", "3"])):
        run_once(cfg(), [FakeAdapter([listing(x) for x in items])], [bad, good], store,
                 now=NOW + timedelta(minutes=5 * i))
    assert any("[监控异常]" in t for t in good.texts)


def test_saturated_page_alerts(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    a = FakeAdapter([listing("1")]); a.saturated = True
    run_once(cfg(), [a], [n], store, now=NOW + timedelta(minutes=5))
    assert any("新增量异常" in t for t in n.texts)


def test_state_save_failure_records_error(tmp_path):
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [FakeNotifier()], BrokenStore(), now=NOW)
    assert any("state" in e for e in r.errors)


# ---------------------------------------------------------------------------
# Ruling coverage
# ---------------------------------------------------------------------------

class RecordingStore:
    """In-memory store that counts writes and never shares its dict."""
    def __init__(self):
        self.state = new_state()
        self.save_calls = 0
    def load(self):
        return copy.deepcopy(self.state)
    def save(self, state):
        self.save_calls += 1
        self.state = copy.deepcopy(state)


class RecordingAdapter(FakeAdapter):
    def __init__(self, items):
        super().__init__(items)
        self.known = None
    def fetch(self, known_ids=frozenset()):
        self.known = known_ids
        return super().fetch(known_ids)


class BatchNotifier(FakeNotifier):
    def __init__(self, name="feishu"):
        super().__init__(name)
        self.batch_sizes = []
    def send(self, items):
        self.batch_sizes.append(len(items))
        super().send(items)


class SendFailsTextWorks(FakeNotifier):
    """Fails item batches but can still receive text alerts."""
    def __init__(self, name="feishu"):
        super().__init__(name)
        self.send_calls = 0
    def send(self, items):
        self.send_calls += 1
        raise NotifyError("send down")


def test_fetch_failure_persists_anchor_and_counter(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    r = run_once(cfg(), [FailingAdapter()], [FakeNotifier()], store, now=NOW)
    site = store.load()["sites"]["jiaoyimao"]
    assert site["anchor_at"] is not None
    assert site["consecutive_failures"] == 1
    assert site["seen"] == {}
    assert r.bootstrap is False and r.errors


def test_failure_counter_caps_and_alerts_once(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    alerts = 0
    for i in range(5):
        r = run_once(cfg(), [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=5 * i))
        alerts += r.alerts_sent
    assert store.load()["sites"]["jiaoyimao"]["consecutive_failures"] == 3
    assert alerts == 1
    assert sum("[监控异常]" in t for t in n.texts) == 1


def test_site_alert_is_throttled(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    c = cfg(threshold=2, interval=180)
    run_once(c, [FakeAdapter([listing("1")])], [n], store, now=NOW)
    run_once(c, [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=5))
    r2 = run_once(c, [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=10))
    assert r2.alerts_sent == 1
    run_once(c, [FakeAdapter([listing("1")])], [n], store, now=NOW + timedelta(minutes=15))
    run_once(c, [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=20))
    r5 = run_once(c, [FailingAdapter()], [n], store, now=NOW + timedelta(minutes=25))
    assert r5.alerts_sent == 0
    assert sum("[监控异常]" in t for t in n.texts) == 1


def test_channel_alert_excludes_failed_channel(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    bad, good = SendFailsTextWorks("feishu"), FakeNotifier("email")
    c = cfg(threshold=2)
    for i, ids in enumerate((["1"], ["1", "2"], ["1", "2", "3"])):
        r = run_once(c, [FakeAdapter([listing(x) for x in ids])], [bad, good], store,
                     now=NOW + timedelta(minutes=5 * i))
    assert any("[监控异常]" in t for t in good.texts)
    assert not any("[监控异常]" in t for t in bad.texts)
    assert r.alerts_sent == 1


def test_site_alert_without_healthy_channel_records_error(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    broken = FakeNotifier(fail=True)
    c = cfg(threshold=2)
    run_once(c, [FakeAdapter([listing("1")])], [broken], store, now=NOW)
    run_once(c, [FailingAdapter()], [broken], store, now=NOW + timedelta(minutes=5))
    r = run_once(c, [FailingAdapter()], [broken], store, now=NOW + timedelta(minutes=10))
    assert r.alerts_sent == 0
    assert any("healthy" in e for e in r.errors)


def test_dry_run_never_saves():
    store = RecordingStore(); n = FakeNotifier()
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW, dry_run=True)
    assert r.bootstrap is True
    assert store.save_calls == 0 and n.items == [] and n.texts == []


def test_no_channels_item_batch_fails_without_saving():
    store = RecordingStore()
    run_once(cfg(), [FakeAdapter([listing("1")])], [FakeNotifier()], store, now=NOW)
    saves = store.save_calls
    r = run_once(cfg(), [FakeAdapter([listing("1"), listing("2")])], [], store,
                 now=NOW + timedelta(minutes=5))
    assert r.new == 1 and r.notified == 0
    assert any("channel" in e.lower() for e in r.errors)
    assert store.save_calls == saves
    assert "2" not in store.state["sites"]["jiaoyimao"]["seen"]


def test_bootstrap_without_channels_records_error_but_saves():
    store = RecordingStore()
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [], store, now=NOW)
    assert r.bootstrap is True and r.errors
    assert store.save_calls == 1
    assert "1" in store.state["sites"]["jiaoyimao"]["seen"]


def test_no_save_when_nothing_substantive_changes():
    store = RecordingStore(); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    saves = store.save_calls
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [n], store,
                 now=NOW + timedelta(minutes=5))
    assert r.new == 0 and r.notified == 0
    assert store.save_calls == saves


def test_heartbeat_commits_without_changes():
    store = RecordingStore(); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    later = NOW + timedelta(hours=25)
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=later)
    assert store.save_calls == 2
    assert store.state["last_commit_at"] == later.isoformat()


def test_filters_applied_after_dedup_and_all_ids_marked(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    c = Config(
        sites={"jiaoyimao": SiteConfig("jiaoyimao", True, ("u",), 1,
                                       Filters(price_max=50))},
        notify=NotifyConfig(("feishu",), 10),
        alerts=AlertConfig(3, 180), bootstrap_notify=True)
    cheap = Listing("jiaoyimao", "2", "商品2", 50, "u", NOW, {"platform": "安卓"})
    pricey = Listing("jiaoyimao", "3", "商品3", 51, "u", NOW, {"platform": "安卓"})
    r = run_once(c, [FakeAdapter([cheap, pricey])], [n], store,
                 now=NOW + timedelta(minutes=5))
    assert r.new == 1 and [x.item_id for x in n.items] == ["2"]
    assert {"2", "3"} <= set(store.load()["sites"]["jiaoyimao"]["seen"])


def test_batches_respect_batch_size(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = BatchNotifier()
    c = cfg(batch_size=2)
    run_once(c, [FakeAdapter([listing("0")])], [n], store, now=NOW)
    items = [listing(str(i)) for i in range(1, 6)]
    r = run_once(c, [FakeAdapter(items)], [n], store, now=NOW + timedelta(minutes=5))
    assert n.batch_sizes == [2, 2, 1]
    assert r.new == 5 and r.notified == 5


def test_fetch_receives_known_ids(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1"), listing("2")])], [n], store, now=NOW)
    adapter = RecordingAdapter([])
    run_once(cfg(), [adapter], [n], store, now=NOW + timedelta(minutes=5))
    assert adapter.known == frozenset({"1", "2"})


class BrowserFailingAdapter(FailingAdapter):
    def fetch(self, known_ids=frozenset()):
        self.used_browser = True
        return super().fetch(known_ids)


def test_used_browser_reported_even_when_fetch_fails(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    r = run_once(cfg(), [BrowserFailingAdapter()], [FakeNotifier()], store, now=NOW)
    assert r.used_browser is True and r.errors


def test_enabled_site_without_adapter_records_error(tmp_path):
    store = FileStateStore(tmp_path / "s.json")
    r = run_once(cfg(), [], [FakeNotifier()], store, now=NOW)
    assert any("jiaoyimao" in e for e in r.errors)


class NeverAdapter:
    site_id = "other"
    used_browser = False
    saturated = False
    def __init__(self): self.fetched = False
    def fetch(self, known_ids=frozenset()):
        self.fetched = True
        return []


def test_disabled_site_is_not_fetched(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    other = NeverAdapter()
    c = Config(
        sites={
            "jiaoyimao": SiteConfig("jiaoyimao", True, ("u",), 1, Filters()),
            "other": SiteConfig("other", False, ("u",), 1, Filters()),
        },
        notify=NotifyConfig(("feishu",), 10),
        alerts=AlertConfig(3, 180), bootstrap_notify=True)
    r = run_once(c, [FakeAdapter([listing("1")]), other], [n], store, now=NOW)
    assert other.fetched is False
    assert "other" not in store.load()["sites"]
    assert not any("other" in e for e in r.errors)


class BrokenLoadStore:
    def load(self): raise StateError("disk")
    def save(self, state): raise AssertionError("must not save")


def test_state_load_error_propagates():
    with pytest.raises(StateError):
        run_once(cfg(), [FakeAdapter([listing("1")])], [FakeNotifier()],
                 BrokenLoadStore(), now=NOW)


# ---------------------------------------------------------------------------
# Fix round 1: per-site state errors and full seen registration
# ---------------------------------------------------------------------------

def test_steady_state_seen_timestamp_refresh_does_not_save():
    store = RecordingStore(); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    saves = store.save_calls
    first_ts = store.state["sites"]["jiaoyimao"]["seen"]["1"]
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [n], store,
                 now=NOW + timedelta(minutes=5))
    assert r.new == 0
    assert store.save_calls == saves
    # mark_seen refreshed the in-memory timestamp, but no save happened.
    assert store.state["sites"]["jiaoyimao"]["seen"]["1"] == first_ts


def test_continuously_listed_item_not_pruned_or_renotified():
    store = RecordingStore()
    state = new_state()
    site = ensure_site(state, "jiaoyimao", NOW - timedelta(days=90))
    site["seen"] = {"1793": (NOW - timedelta(days=61)).isoformat()}
    store.state = state

    n = FakeNotifier()
    old = listing("1793", published=NOW - timedelta(days=80))
    r1 = run_once(cfg(), [FakeAdapter([old])], [n], store, now=NOW)
    assert r1.new == 0 and n.items == []
    assert "1793" in store.state["sites"]["jiaoyimao"]["seen"]

    r2 = run_once(cfg(), [FakeAdapter([old])], [n], store,
                  now=NOW + timedelta(minutes=5))
    assert r2.new == 0 and n.items == []


def test_corrupt_site_timestamp_does_not_abort_other_sites():
    store = RecordingStore()
    state = new_state()
    broken = ensure_site(state, "broken", NOW)
    broken["last_alert_at"] = "not-a-timestamp"
    store.state = state

    class BrokenSiteAdapter(FailingAdapter):
        site_id = "broken"

    c = Config(
        sites={
            "broken": SiteConfig("broken", True, ("u",), 1, Filters()),
            "jiaoyimao": SiteConfig("jiaoyimao", True, ("u",), 1, Filters()),
        },
        notify=NotifyConfig(("feishu",), 10),
        alerts=AlertConfig(1, 180), bootstrap_notify=True)
    n = FakeNotifier()
    r = run_once(c, [BrokenSiteAdapter(), FakeAdapter([listing("1")])], [n],
                 store, now=NOW)

    assert any("broken" in e and "state" in e for e in r.errors)
    assert r.bootstrap is True
    assert "1" in store.state["sites"]["jiaoyimao"]["seen"]
    assert store.state["sites"]["broken"]["consecutive_failures"] == 1
    assert store.save_calls == 1


# ---------------------------------------------------------------------------
# Detail-page enrichment (安全报告)
# ---------------------------------------------------------------------------

class EnrichingAdapter(FakeAdapter):
    def __init__(self, items, enriched, fail_ids=()):
        super().__init__(items)
        self.enriched = enriched
        self.fail_ids = set(fail_ids)
        self.detail_calls = []

    def fetch_detail(self, item):
        self.detail_calls.append(item.item_id)
        if item.item_id in self.fail_ids:
            raise FetchError("detail blocked")
        return self.enriched[item.item_id]


def _enriched(i, account_transfer="是"):
    from monitor.models import SafeReport
    return Listing(
        "jiaoyimao", i, f"商品{i}", 100, f"https://x/{i}", NOW,
        {"server": "和之国", "platform": "安卓", "account": "小米帐号"},
        ("https://img.test/a.jpg",),
        SafeReport("SSS", "100%", "100% 不会被找回", ("卖家信用认证：认证通过",),
                   account_transfer),
    )


def test_new_items_are_enriched_before_notifying(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter(
        [listing("1"), listing("2")], {"2": _enriched("2")}
    )
    r = run_once(cfg(), [adapter], [n], store, now=NOW + timedelta(minutes=5))

    assert adapter.detail_calls == ["2"]
    assert r.new == 1 and r.warnings == []
    assert n.items[0].safe_report.level == "SSS"
    assert n.items[0].images == ("https://img.test/a.jpg",)


def test_detail_failure_warns_but_still_notifies(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter([listing("1"), listing("2")], {}, fail_ids={"2"})
    r = run_once(cfg(), [adapter], [n], store, now=NOW + timedelta(minutes=5))

    assert r.errors == []
    assert len(r.warnings) == 1 and "detail" in r.warnings[0]
    assert [x.item_id for x in n.items] == ["2"]
    assert n.items[0].safe_report is None
    assert "2" in store.load()["sites"]["jiaoyimao"]["seen"]


def test_bootstrap_does_not_fetch_details(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    adapter = EnrichingAdapter([listing("1")], {"1": _enriched("1")})

    run_once(cfg(), [adapter], [n], store, now=NOW)

    assert adapter.detail_calls == []


def test_require_account_transfer_notifies_only_yes(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter(
        [listing("1"), listing("2"), listing("3"), listing("4")],
        {"2": _enriched("2"), "3": _enriched("3", account_transfer="否")},
        fail_ids={"4"},
    )
    r = run_once(cfg(filters=Filters(require_account_transfer=True)),
                 [adapter], [n], store, now=NOW + timedelta(minutes=5))

    assert [x.item_id for x in n.items] == ["2"]
    assert r.new == 1 and r.notified == 1
    assert any("账号转移" in w for w in r.warnings)
    seen = store.load()["sites"]["jiaoyimao"]["seen"]
    assert {"2", "3"} <= set(seen)
    assert "4" not in seen  # unknown status: retried on the next run


def test_unknown_account_transfer_is_retried_next_run(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    f = Filters(require_account_transfer=True)

    failing = EnrichingAdapter([listing("1"), listing("2")], {}, fail_ids={"2"})
    run_once(cfg(filters=f), [failing], [n], store, now=NOW + timedelta(minutes=5))
    assert n.items == []

    adapter = EnrichingAdapter([listing("1"), listing("2")], {"2": _enriched("2")})
    r = run_once(cfg(filters=f), [adapter], [n], store,
                 now=NOW + timedelta(minutes=10))

    assert adapter.detail_calls == ["2"]
    assert [x.item_id for x in n.items] == ["2"] and r.notified == 1


def test_require_account_transfer_off_notifies_everything(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter(
        [listing("1"), listing("2")],
        {"2": _enriched("2", account_transfer="否")},
    )
    r = run_once(cfg(), [adapter], [n], store, now=NOW + timedelta(minutes=5))

    assert [x.item_id for x in n.items] == ["2"] and r.new == 1


def test_require_account_transfer_dry_run_warns(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter([listing("1"), listing("2")], {})
    r = run_once(cfg(filters=Filters(require_account_transfer=True)),
                 [adapter], [n], store, now=NOW + timedelta(minutes=5),
                 dry_run=True)

    assert adapter.detail_calls == []
    assert any("账号转移" in w for w in r.warnings)


def test_dry_run_does_not_fetch_details(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)

    adapter = EnrichingAdapter(
        [listing("1"), listing("2")], {"2": _enriched("2")}
    )
    r = run_once(cfg(), [adapter], [n], store, now=NOW + timedelta(minutes=5),
                 dry_run=True)

    assert adapter.detail_calls == []
    assert [x.item_id for x in r.would_notify] == ["2"]
