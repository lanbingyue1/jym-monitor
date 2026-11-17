import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from monitor.errors import StateError
from monitor.models import Listing
from monitor.state import (
    FileStateStore,
    GitHubStateStore,
    ensure_notifier,
    ensure_site,
    is_new,
    mark_seen,
    new_state,
    prune_seen,
    should_commit,
)

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def L(item_id, published):
    return Listing("jiaoyimao", item_id, "t", 100, "u", published, {})


def test_anchor_and_new_predicate():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    assert s["anchor_at"] == NOW.isoformat()
    n = ensure_notifier(st, "feishu")
    assert n["consecutive_failures"] == 0
    old = L("1793434810016540", NOW - timedelta(days=10))
    # Brief 原值 NOW - 5min 与 "published_at >= anchor_at" 规则矛盾
    # （anchor_at == NOW）；改为 anchor 之后的时间以匹配规则。
    new = L("1794841605849819", NOW + timedelta(minutes=5))
    unknown = L("1794841605849819", None)
    assert not is_new(old, s)
    assert is_new(new, s)
    assert is_new(unknown, s)  # 缺失时间保守提醒


def test_new_predicate_includes_anchor_instant():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    assert is_new(L("1", NOW), s)
    assert not is_new(L("2", NOW - timedelta(seconds=1)), s)


def test_mark_seen_and_prune():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    mark_seen(s, [L("1", None), L("2", None)], NOW)
    assert set(s["seen"]) == {"1", "2"}
    old = (NOW - timedelta(days=61)).isoformat()
    s["seen"] = {"old": old, "x": NOW.isoformat()}
    prune_seen(s, NOW)
    assert "old" not in s["seen"] and "x" in s["seen"]


def test_file_store_roundtrip(tmp_path):
    store = FileStateStore(tmp_path / "state.json")
    st = new_state()
    store.save(st)
    assert store.load() == st
    assert FileStateStore(tmp_path / "missing.json").load() == new_state()


def test_github_store_read_and_write(monkeypatch):
    calls = []

    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_get(url, **kw):
        calls.append(("GET", url))
        body = base64.b64encode(json.dumps({"version": 1}).encode()).decode()
        return R({"content": body, "sha": "abc"})

    def fake_put(url, **kw):
        calls.append(("PUT", url, kw.get("json")))
        return R({"content": {"sha": "def"}})

    monkeypatch.setattr("monitor.state.requests.get", fake_get)
    monkeypatch.setattr("monitor.state.requests.put", fake_put)
    store = GitHubStateStore("o/r", "tok")
    assert store.load() == {"version": 1}
    store.save({"version": 1, "sites": {}})
    assert calls[1][2]["sha"] == "abc"
    assert "message" in calls[1][2]


def test_github_store_missing_file_returns_new_state(monkeypatch):
    class R:
        status_code = 404

        def json(self):
            return {}

    monkeypatch.setattr("monitor.state.requests.get", lambda *a, **k: R())
    assert GitHubStateStore("o/r", "tok").load() == new_state()


def test_github_store_retries_once_on_conflict(monkeypatch):
    puts = {"n": 0}

    class R:
        def __init__(self, status, payload=None):
            self.status_code, self._p = status, payload or {}

        def json(self):
            return self._p

    def fake_get(*a, **k):
        body = base64.b64encode(b'{"version": 1}').decode()
        return R(200, {"content": body, "sha": "new"})

    def fake_put(*a, **k):
        puts["n"] += 1
        return R(409) if puts["n"] == 1 else R(200, {"content": {"sha": "def"}})

    monkeypatch.setattr("monitor.state.requests.get", fake_get)
    monkeypatch.setattr("monitor.state.requests.put", fake_put)
    store = GitHubStateStore("o/r", "tok")
    store.load()
    store.save({"version": 1})  # 第一次 409，重读 sha 后第二次成功
    assert puts["n"] == 2


def test_github_store_persistent_conflict_raises(monkeypatch):
    class R:
        def __init__(self, status, payload=None):
            self.status_code, self._p = status, payload or {}

        def json(self):
            return self._p

    body = base64.b64encode(b"{}").decode()
    monkeypatch.setattr(
        "monitor.state.requests.get", lambda *a, **k: R(200, {"content": body, "sha": "s"})
    )
    monkeypatch.setattr("monitor.state.requests.put", lambda *a, **k: R(409))
    with pytest.raises(StateError):
        GitHubStateStore("o/r", "tok").save({"version": 1})


def test_should_commit_heartbeat():
    st = new_state()
    st["last_commit_at"] = (NOW - timedelta(hours=25)).isoformat()
    assert should_commit(st, NOW) is True
    st["last_commit_at"] = NOW.isoformat()
    assert should_commit(st, NOW) is False


# --- supplemental tests for the resolved ambiguities -------------------------


def test_should_commit_when_last_commit_missing():
    assert should_commit(new_state(), NOW) is True


def test_ensure_site_preserves_existing_values_and_fills_missing():
    old_anchor = (NOW - timedelta(days=3)).isoformat()
    st = new_state()
    st["sites"]["jiaoyimao"] = {"anchor_at": old_anchor}
    s = ensure_site(st, "jiaoyimao", NOW)
    assert s["anchor_at"] == old_anchor
    assert s["seen"] == {}
    assert s["last_run"] is None
    assert s["consecutive_failures"] == 0
    assert s["last_alert_at"] is None

    st["notifiers"]["feishu"] = {"last_alert_at": NOW.isoformat()}
    n = ensure_notifier(st, "feishu")
    assert n["last_alert_at"] == NOW.isoformat()
    assert n["consecutive_failures"] == 0


def test_new_state_shape():
    assert new_state() == {
        "version": 1,
        "last_commit_at": None,
        "sites": {},
        "notifiers": {},
    }


def test_prune_seen_accepts_z_and_naive_timestamps():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    s["seen"] = {
        "z": "2026-11-15T15:00:00Z",
        "naive": "2026-11-15T15:00:00",
        "stale": "2026-01-01T00:00:00Z",
    }
    prune_seen(s, NOW)
    assert set(s["seen"]) == {"z", "naive"}


def test_prune_seen_drops_unparseable_timestamps():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    s["seen"] = {"bad": "not-a-timestamp", "good": NOW.isoformat()}
    prune_seen(s, NOW)
    assert s["seen"] == {"good": NOW.isoformat()}


def test_prune_seen_keeps_newest_when_over_max_entries():
    st = new_state()
    s = ensure_site(st, "jiaoyimao", NOW)
    s["seen"] = {
        f"id{i}": (NOW - timedelta(minutes=i)).isoformat() for i in range(5)
    }
    prune_seen(s, NOW, max_entries=2)
    assert set(s["seen"]) == {"id0", "id1"}


def test_file_store_rejects_malformed_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        FileStateStore(path).load()


def test_file_store_rejects_non_dict_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(StateError):
        FileStateStore(path).load()


def test_file_store_leaves_no_temp_files(tmp_path):
    store = FileStateStore(tmp_path / "state.json")
    store.save(new_state())
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_github_store_non_200_raises(monkeypatch):
    class R:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr("monitor.state.requests.get", lambda *a, **k: R())
    with pytest.raises(StateError):
        GitHubStateStore("o/r", "tok").load()


def test_github_store_empty_content_returns_new_state(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"content": "", "sha": "x"}

    monkeypatch.setattr("monitor.state.requests.get", lambda *a, **k: R())
    assert GitHubStateStore("o/r", "tok").load() == new_state()


def test_github_store_rejects_corrupt_content(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"content": base64.b64encode(b"not json").decode(), "sha": "x"}

    monkeypatch.setattr("monitor.state.requests.get", lambda *a, **k: R())
    with pytest.raises(StateError):
        GitHubStateStore("o/r", "tok").load()


def test_github_store_headers_timeout_and_put_body(monkeypatch):
    calls = []

    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    body = base64.b64encode(b'{"version": 1}').decode()

    def fake_get(url, **kw):
        calls.append(("GET", url, kw))
        return R({"content": body, "sha": "abc"})

    def fake_put(url, **kw):
        calls.append(("PUT", url, kw))
        return R({"content": {"sha": "def"}})

    monkeypatch.setattr("monitor.state.requests.get", fake_get)
    monkeypatch.setattr("monitor.state.requests.put", fake_put)
    store = GitHubStateStore("o/r", "tok", branch="mybranch", path="data/state.json")
    store.load()
    state = {"version": 1, "title": "中文"}
    store.save(state)

    get_url, get_kw = calls[0][1], calls[0][2]
    put_url, put_kw = calls[1][1], calls[1][2]
    assert "/repos/o/r/contents/data/state.json" in get_url
    assert "ref=mybranch" in get_url
    for kw in (get_kw, put_kw):
        assert kw["headers"]["Authorization"] == "Bearer tok"
        assert kw["headers"]["Accept"] == "application/vnd.github+json"
        assert kw["timeout"] == 30
    assert "/repos/o/r/contents/data/state.json" in put_url
    payload = put_kw["json"]
    assert payload["branch"] == "mybranch"
    assert payload["sha"] == "abc"
    assert "message" in payload
    decoded = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    assert decoded == state
