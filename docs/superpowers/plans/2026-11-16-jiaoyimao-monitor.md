# 交易猫新品监控告警器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 GitHub Actions 上每 5 分钟检查交易猫「忍者必须死3 → 成品号」列表，发现新上架商品后通过飞书或邮件推送，去重可靠、故障可见、通知与站点均可插拔。

**Architecture:** Python 单进程流水线：站点适配器抓取并归一化为 `Listing`，流水线做「锚点 + 已见 ID」判新、规则过滤、批量通知，成功后把状态写回 `state` 分支。HTTP 优先，风控时降级到无头 Chrome。

**Tech Stack:** Python 3.12、requests、PyYAML、playwright（仅兜底）、pytest、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-11-16-jiaoyimao-monitor-design.md`

## Global Constraints

- Python 3.12；依赖只允许：`requests`、`PyYAML`、`playwright`、`pytest`。
- 仓库公开；**任何密钥不得写入代码或配置文件**，只从环境变量读取：`FEISHU_WEBHOOK`、`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`、`HEALTHCHECK_PING_URL`。
- 状态存 `state` 分支的 `state.json`，通过 GitHub Contents API 读写；workflow 权限 `contents: write`。
- 定时 `cron "*/5 * * * *"` + `workflow_dispatch`；`concurrency.group = monitor`。
- 每个列表 URL 最多翻 3 页；页间等待 1.5 秒；不做登录、不破解验证码。
- 所有测试离线运行，网络调用必须 mock；测试命令统一用 `python -m pytest tests -v`。
- TDD：每个任务先写失败测试，再实现，最后提交一次。

## Review Focus

1. **风控页伪装成正常响应**：HTTP 200 但内容是 `x5secdata` 滑块页时，必须抛 `FetchError`，绝不能被当成「没有新品」。
2. **SSR 数据含 `undefined`**：`window.__INITIAL_DATA__` 不是严格 JSON，解析必须容忍 `:undefined` 与尾随内容。
3. **合法的空列表 vs 解析失败**：真正的空 `goodsList` 返回 `[]`；结构不符/找不到数据必须抛错并告警。
4. **通知失败后的状态语义**：全部渠道发送失败时不得把商品标记为已见，下一次运行必须重试。
5. **状态写入失败/冲突**：Contents API 409 时先重新读取 sha 再重试一次；仍失败必须让本次运行报错，不能静默丢掉已见记录。
6. **上架时间缺失或非法**：`goodsId` 前缀无法解析为合理时间时 `published_at=None`，仍应推送，不能崩溃。

---

### Task 1: 项目骨架、数据模型与配置加载

**Files:**
- Create: `requirements.txt`, `.gitignore`, `monitor/__init__.py`, `monitor/models.py`, `monitor/config.py`, `config.yaml`, `tests/__init__.py`（空文件）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Listing(site: str, item_id: str, title: str, price: int | None, url: str, published_at: datetime | None, extra: Mapping[str, str])`
  - `Filters(price_min, price_max, platforms, servers, include_keywords, exclude_keywords)`
  - `SiteConfig(site_id, enabled, urls: tuple[str, ...], max_pages: int, filters: Filters)`
  - `NotifyConfig(channels: tuple[str, ...], batch_size: int)`
  - `AlertConfig(consecutive_failures_before_alert: int, min_alert_interval_minutes: int)`
  - `Config(sites: Mapping[str, SiteConfig], notify: NotifyConfig, alerts: AlertConfig, bootstrap_notify: bool)`
  - `load_config(path: str | Path) -> Config`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
from pathlib import Path
from monitor.config import load_config, Filters

def test_load_example_config(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        """
sites:
  jiaoyimao:
    enabled: true
    urls: ["https://example.com/a"]
    max_pages: 3
    filters:
      price_min: 50
      price_max: 800
      platforms: ["安卓"]
      servers: []
      include_keywords: ["血影"]
      exclude_keywords: []
notify:
  channels: [feishu, email]
  batch_size: 10
alerts:
  consecutive_failures_before_alert: 3
  min_alert_interval_minutes: 180
bootstrap:
  notify_on_first_run: true
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    site = cfg.sites["jiaoyimao"]
    assert site.max_pages == 3
    assert site.filters.price_min == 50
    assert site.filters.platforms == ("安卓",)
    assert cfg.notify.channels == ("feishu", "email")
    assert cfg.bootstrap_notify is True

def test_minimal_config_uses_defaults(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites:\n  jiaoyimao:\n    urls: [u]\n", encoding="utf-8")
    cfg = load_config(p)
    site = cfg.sites["jiaoyimao"]
    assert site.enabled is True and site.max_pages == 3
    assert site.filters == Filters()
    assert cfg.notify.batch_size == 10
    assert cfg.alerts.consecutive_failures_before_alert == 3
    assert cfg.bootstrap_notify is True
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: monitor.config`）

- [ ] **Step 3: 实现**

`requirements.txt`：

```
requests>=2.32
PyYAML>=6.0
playwright>=1.50
pytest>=8.0
```

`.gitignore`：

```
__pycache__/
.pytest_cache/
state.local.json
*.pyc
```

`monitor/models.py` 用 `@dataclass(frozen=True)` 定义 `Listing`（`extra: Mapping[str, str] = field(default_factory=dict)`）。

`monitor/config.py`：所有 dataclass `frozen=True`；`load_config` 用 `yaml.safe_load`，缺省值：`enabled=True`、`max_pages=3`、`batch_size=10`、`consecutive_failures_before_alert=3`、`min_alert_interval_minutes=180`、`bootstrap_notify=True`；列表字段转 tuple；未知渠道名抛 `ValueError`。

`config.yaml` 按设计文档 §6.4 写第一版配置（`filters` 全空；`channels: [feishu]`；`max_pages: 3`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add requirements.txt .gitignore monitor config.yaml tests
git commit -m "feat: project skeleton, models and config loader"
```

---

### Task 2: 交易猫 HTML 解析器

**Files:**
- Create: `monitor/sites/__init__.py`（先留空）、`monitor/sites/jiaoyimao.py`、`tests/fixtures/jiaoyimao_list.html`、`tests/test_jiaoyimao_parser.py`
- Modify: none

**Interfaces:**
- Produces:
  - `parse_published_at(item_id: str, now: datetime | None = None) -> datetime | None`
  - `parse_listings(html: str) -> list[Listing]`
  - `is_punish(html: str) -> bool`
  - 本任务解析失败抛 `ValueError`；`FetchError` 在 Task 3 创建。

- [ ] **Step 1: 准备真实 fixture**

```bash
mkdir -p tests/fixtures
cp /c/Users/tao/AppData/Local/Temp/jym-spike/search_page.html tests/fixtures/jiaoyimao_list.html
```

该页面含 16 条真实商品、一个 `type == 14` 的安全提示条，并含有 `keyword_type":undefined`，覆盖 Review Focus 第 2 条。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_jiaoyimao_parser.py
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
```

- [ ] **Step 3: 运行并确认失败**

Run: `python -m pytest tests/test_jiaoyimao_parser.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现**

`monitor/sites/jiaoyimao.py`：

- `is_punish(html)`：前 6000 字符含 `x5secdata` 或 `_____tmd_____/punish`。
- `parse_published_at`：`item_id[:13]` 转 int/1000 为 UTC；合理范围 2020-01-01 至 `now + 1 天`，否则 `None`。
- `parse_listings`：定位 `window.__INITIAL_DATA__`，取其后第一个 `{`；用 `re.sub(r"(?<=:)\s*undefined(?=\s*[,}])", "null", raw)` 后用 `json.JSONDecoder().raw_decode`；找不到/解析失败抛 `ValueError`；取 `goodsListData.goodsList` 中 `type == 8` 的 `data`；`goodsId` 在 JSON 里是整数，必须 `str()`；`extra` 至少包含 `server`、`platform`、`description`、`sell_points`（`sellPoints[].desc` 用空格连接）；`url` 用 `detailUrl`，缺失时拼 `https://www.jiaoyimao.com/jg1008016/{goodsId}.html`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_jiaoyimao_parser.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add monitor/sites tests/fixtures tests/test_jiaoyimao_parser.py
git commit -m "feat: jiaoyimao SSR parser"
```

---

### Task 3: 抓取、风控检测与浏览器降级

**Files:**
- Create: `monitor/errors.py`
- Modify: `monitor/sites/jiaoyimao.py`
- Test: `tests/test_jiaoyimao_fetch.py`

**Interfaces:**
- Produces:
  - `FetchError(Exception)`、`NotifyError(Exception)`、`StateError(Exception)`、`ConfigError(ValueError)`
  - `fetch_html(url: str, session: requests.Session | None = None) -> str`
  - `fetch_with_browser(url: str) -> str`
- Consumes: `is_punish`（Task 2）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jiaoyimao_fetch.py
import pytest
import requests
from monitor.errors import FetchError
from monitor.sites import jiaoyimao

class FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError()

def test_punish_response_raises(monkeypatch):
    monkeypatch.setattr(jiaoyimao.requests, "get",
                        lambda *a, **k: FakeResp('<script>x5secdata</script>'))
    monkeypatch.setattr(jiaoyimao.time, "sleep", lambda s: None)
    with pytest.raises(FetchError):
        jiaoyimao.fetch_html("https://example.com")

def test_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}
    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResp("x5secdata") if calls["n"] == 1 else FakeResp("<html>ok</html>")
    monkeypatch.setattr(jiaoyimao.requests, "get", fake_get)
    monkeypatch.setattr(jiaoyimao.time, "sleep", lambda s: None)
    assert jiaoyimao.fetch_html("https://example.com") == "<html>ok</html>"
    assert calls["n"] == 2
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_jiaoyimao_fetch.py -v`
Expected: FAIL（`monitor.errors` 不存在）

- [ ] **Step 3: 实现**

`monitor/errors.py`：`FetchError`、`NotifyError`、`StateError` 继承 `Exception`；`ConfigError` 继承 `ValueError`。

`fetch_html`：浏览器请求头（Chrome UA、`Accept-Language: zh-CN`、`Referer: https://www.jiaoyimao.com/`），`timeout=30`；`is_punish` 或非 2xx 时等待 3 秒重试一次；仍失败抛 `FetchError`。

`fetch_with_browser`：函数内部 `from playwright.sync_api import sync_playwright`；`channel="chrome"`, `headless=True`，`--disable-blink-features=AutomationControlled`，注入脚本去掉 `navigator.webdriver`；`goto(..., wait_until="domcontentloaded", timeout=90000)`，等待 6 秒后取 `page.content()`；若结果仍 `is_punish` 则抛 `FetchError`；其它异常包装为 `FetchError`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_jiaoyimao_fetch.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/errors.py monitor/sites/jiaoyimao.py tests/test_jiaoyimao_fetch.py
git commit -m "feat: http fetch with anti-bot detection and browser fallback"
```

---

### Task 4: 站点适配器与翻页

**Files:**
- Create: `monitor/sites/base.py`
- Modify: `monitor/sites/jiaoyimao.py`, `monitor/sites/__init__.py`
- Test: `tests/test_jiaoyimao_adapter.py`

**Interfaces:**
- Produces:
  - `class SiteAdapter(Protocol): site_id: str; def fetch(self, known_ids: frozenset[str] = frozenset()) -> list[Listing]`
  - `JiaoyimaoAdapter(cfg: SiteConfig, http_fetch=fetch_html, browser_fetch=fetch_with_browser)`；实例属性 `used_browser: bool`、`saturated: bool`
  - `build_adapters(config: Config) -> list[SiteAdapter]`
- Consumes: `SiteConfig`（Task 1）、`parse_listings` / `FetchError`（Task 2/3）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jiaoyimao_adapter.py
from pathlib import Path
from monitor.config import SiteConfig, Filters
from monitor.sites.jiaoyimao import JiaoyimaoAdapter

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "jiaoyimao_list.html").read_text(encoding="utf-8")

def make_adapter(htmls, calls):
    def http_fetch(url):
        calls.append(url)
        return htmls[min(len(calls) - 1, len(htmls) - 1)]
    return JiaoyimaoAdapter(
        SiteConfig(site_id="jiaoyimao", enabled=True, urls=("https://example.com/list",),
                   max_pages=3, filters=Filters()),
        http_fetch=http_fetch,
    )

def test_stops_paging_when_known_id_seen():
    calls = []
    a = make_adapter([FIXTURE_HTML], calls)
    items = a.fetch(known_ids=frozenset({"1793434810016540"}))
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
    def bad_fetch(url): raise FetchError("punish")
    calls = []
    a = JiaoyimaoAdapter(
        SiteConfig(site_id="jiaoyimao", enabled=True, urls=("https://example.com/list",),
                   max_pages=1, filters=Filters()),
        http_fetch=bad_fetch,
        browser_fetch=lambda url: calls.append(url) or FIXTURE_HTML,
    )
    items = a.fetch()
    assert len(items) == 16 and len(calls) == 1 and a.used_browser is True
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_jiaoyimao_adapter.py -v`
Expected: FAIL（`JiaoyimaoAdapter` 不存在）

- [ ] **Step 3: 实现**

- 每轮 `fetch` 开始时重置 `used_browser = False`、`saturated = False`。
- 对 `cfg.urls` 逐个抓取；每个 URL 从 `page=1` 开始，用 `urllib.parse` 加/替换 `page` 参数；页间 `time.sleep(1.5)`。
- 每页调用 `parse_listings`；`ValueError` 包装为 `FetchError`（Review Focus 第 3 条：解析失败不能当成空列表）。
- 条目按 `item_id` 跨页去重；若本页出现 `known_ids` 中的 ID 则停止翻页；否则继续到 `cfg.max_pages`。
- 若翻到 `max_pages` 且该页仍有 16 条（`PAGE_SIZE = 16`）且没有 `known_ids`，置 `saturated = True`（翻页可能不够，流水线会告警）。
- `http_fetch` 抛 `FetchError` 时调用 `browser_fetch` 并置 `used_browser = True`；浏览器也失败则继续抛 `FetchError`。

`build_adapters`：遍历 `config.sites.values()`，`enabled` 且 `site_id == "jiaoyimao"` 时构造适配器；未知站点名抛 `ConfigError`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_jiaoyimao_adapter.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/sites tests/test_jiaoyimao_adapter.py
git commit -m "feat: jiaoyimao adapter with pagination and browser fallback"
```

---

### Task 5: 状态模型与存储

**Files:**
- Create: `monitor/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `new_state() -> dict`
  - `FileStateStore(path)`: `load() -> dict`, `save(state: dict) -> None`
  - `GitHubStateStore(repo: str, token: str, branch: str = "state", path: str = "state.json")`: 同上
  - `ensure_site(state, site_id, now) -> dict`
  - `ensure_notifier(state, name) -> dict`
  - `is_new(listing, site_state) -> bool`
  - `mark_seen(site_state, listings, now) -> None`
  - `prune_seen(site_state, now, days=60, max_entries=5000) -> None`
  - `should_commit(state, now, heartbeat_hours=24) -> bool`
- Consumes: `Listing`（Task 1）、`StateError`（Task 3）

状态 schema（在本任务中固定）：

```json
{
  "version": 1,
  "last_commit_at": "2026-11-16T15:40:00+00:00",
  "sites": {
    "jiaoyimao": {
      "anchor_at": "2026-11-16T15:37:00+00:00",
      "seen": {"1794831310167680": "2026-11-16T15:37:00+00:00"},
      "last_run": "2026-11-16T15:45:00+00:00",
      "consecutive_failures": 0,
      "last_alert_at": null
    }
  },
  "notifiers": {"feishu": {"consecutive_failures": 0, "last_alert_at": null}}
}
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_state.py
import base64, json
from datetime import datetime, timezone, timedelta
import pytest
from monitor.errors import StateError
from monitor.state import (new_state, FileStateStore, GitHubStateStore, ensure_site,
                           ensure_notifier, is_new, mark_seen, prune_seen, should_commit)
from monitor.models import Listing

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
    new = L("1794841605849819", NOW - timedelta(minutes=5))
    unknown = L("1794841605849819", None)
    assert not is_new(old, s)
    assert is_new(new, s)
    assert is_new(unknown, s)  # 缺失时间保守提醒

def test_mark_seen_and_prune():
    st = new_state(); s = ensure_site(st, "jiaoyimao", NOW)
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
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
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
        def json(self): return {}
    monkeypatch.setattr("monitor.state.requests.get", lambda *a, **k: R())
    assert GitHubStateStore("o/r", "tok").load() == new_state()

def test_github_store_retries_once_on_conflict(monkeypatch):
    puts = {"n": 0}
    class R:
        def __init__(self, status, payload=None): self.status_code, self._p = status, payload or {}
        def json(self): return self._p
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
        def __init__(self, status, payload=None): self.status_code, self._p = status, payload or {}
        def json(self): return self._p
    body = base64.b64encode(b"{}").decode()
    monkeypatch.setattr("monitor.state.requests.get",
                        lambda *a, **k: R(200, {"content": body, "sha": "s"}))
    monkeypatch.setattr("monitor.state.requests.put", lambda *a, **k: R(409))
    with pytest.raises(StateError):
        GitHubStateStore("o/r", "tok").save({"version": 1})

def test_should_commit_heartbeat():
    st = new_state()
    st["last_commit_at"] = (NOW - timedelta(hours=25)).isoformat()
    assert should_commit(st, NOW) is True
    st["last_commit_at"] = NOW.isoformat()
    assert should_commit(st, NOW) is False
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `new_state()` 返回 `{"version": 1, "last_commit_at": None, "sites": {}, "notifiers": {}}`。
- 时间统一用带时区的 UTC，序列化用 `datetime.isoformat()`，解析兼容结尾 `Z`。
- `ensure_site` 只在缺少 `anchor_at` 时写入；同时补齐 `seen`、`last_run`、`consecutive_failures`、`last_alert_at` 默认值。`ensure_notifier` 同理。
- `is_new(listing, site_state)`：`item_id not in seen` 且（`published_at is None` 或 `published_at >= anchor_at`）。
- `FileStateStore`：不存在时返回 `new_state()`；写入用临时文件 + `os.replace` 原子替换。
- `GitHubStateStore`：`GET https://api.github.com/repos/{repo}/contents/{path}?ref={branch}`，`Authorization: Bearer {token}`，base64 解码、保存 `sha`；404 视为空状态；`PUT` 到同一 URL，body 含 `message`、`content`（base64）、`sha`（如果有）、`branch`；PUT 返回 409 时重新 `load()` 刷新 sha 并重试一次（Review Focus 第 5 条）；仍失败抛 `StateError`。
- `should_commit`：`last_commit_at` 缺失或超过 24 小时返回 True。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/state.py tests/test_state.py
git commit -m "feat: state model with file and GitHub Contents stores"
```

---

### Task 6: 通知适配器

**Files:**
- Create: `monitor/notify/__init__.py`, `monitor/notify/base.py`, `monitor/notify/feishu.py`, `monitor/notify/email.py`
- Test: `tests/test_notifiers.py`

**Interfaces:**
- Produces:
  - `class Notifier(Protocol): name: str; def send(self, items: list[Listing]) -> None; def send_text(self, text: str) -> None`
  - `FeishuNotifier(webhook: str, timeout: float = 10)`
  - `EmailNotifier(host, port, user, password, to: str, use_ssl: bool = True)`
  - `build_notifiers(cfg: NotifyConfig, env: Mapping[str, str]) -> list[Notifier]`
- Consumes: `Listing`（Task 1）、`NotifyError` / `ConfigError`（Task 3）

说明：设计文档中的 `Notifier.alert(text)` 在本计划中统一命名为 `send_text(text)`；`[监控异常]` 前缀由调用方添加，这样首次运行的「监控已启动」也能复用同一接口。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_notifiers.py
import pytest
from monitor.models import Listing
from monitor.notify.feishu import FeishuNotifier
from monitor.notify.email import EmailNotifier
from monitor.notify import build_notifiers
from monitor.config import NotifyConfig
from monitor.errors import NotifyError, ConfigError

ITEM = Listing("jiaoyimao", "1794831310167680", "6000勾玉 一命血影", 100,
               "https://www.jiaoyimao.com/jg1008016/1794831310167680.html", None,
               {"server": "和之国", "platform": "安卓"})

def test_feishu_payload_contains_item(monkeypatch):
    sent = {}
    class R:
        status_code = 200
        def json(self): return {"code": 0}
    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json
        return R()
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([ITEM])
    text = str(sent["json"])
    assert "6000勾玉 一命血影" in text and "¥100" in text
    assert "https://www.jiaoyimao.com/jg1008016/1794831310167680.html" in text

def test_feishu_failure_raises(monkeypatch):
    class R:
        status_code = 500
        def json(self): return {}
    monkeypatch.setattr("monitor.notify.feishu.requests.post", lambda *a, **k: R())
    with pytest.raises(NotifyError):
        FeishuNotifier("https://hook").send_text("x")

def test_email_builds_message(monkeypatch):
    sent = {}
    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent["msg"] = msg
    monkeypatch.setattr("monitor.notify.email.smtplib.SMTP_SSL", SMTP)
    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([ITEM])
    body = sent["msg"].as_string()
    assert "6000勾玉 一命血影" in body and "¥100" in body

def test_build_notifiers_requires_env():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    with pytest.raises(ConfigError):
        build_notifiers(cfg, {})

def test_build_feishu_notifier():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    ns = build_notifiers(cfg, {"FEISHU_WEBHOOK": "https://hook"})
    assert [n.name for n in ns] == ["feishu"]
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_notifiers.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- 飞书 `send`：`msg_type="interactive"`，卡片元素用 `lark_md`，每条：`**[title](url)**\n¥价格 · 服务器：X · 平台：Y`；价格未知写 `价格未知`。`send_text`：`msg_type="text"`。HTTP 非 2xx 或响应 `code != 0` 抛 `NotifyError`。
- 邮件 `send`：`EmailMessage`，主题 `交易猫新品提醒 (N)`，HTML 表格（标题链接、价格、服务器、平台），所有字段用 `html.escape` 转义；`send_text`：纯文本主题 `[监控异常] 交易猫监控`。SMTP 异常抛 `NotifyError`。
- `EmailNotifier.to` 支持逗号分隔多个地址；`use_ssl=True` 用 `smtplib.SMTP_SSL`，否则 `smtplib.SMTP` + `starttls()`。
- `build_notifiers`：按 `cfg.channels` 读取环境变量；缺 `FEISHU_WEBHOOK` 或 SMTP 任一必填项时抛 `ConfigError`；`SMTP_PORT` 转 int，465 走 SSL，其它端口走 STARTTLS。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notifiers.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/notify tests/test_notifiers.py
git commit -m "feat: feishu and email notifiers"
```

---

### Task 7: 主流水线

**Files:**
- Create: `monitor/pipeline.py`, `monitor/filters.py`
- Test: `tests/test_pipeline.py`, `tests/test_filters.py`

**Interfaces:**
- Produces:
  - `matches(listing: Listing, filters: Filters) -> bool`
  - `RunResult(fetched: int, new: int, notified: int, bootstrap: bool, used_browser: bool, errors: list[str], alerts_sent: int)`
  - `run_once(config: Config, adapters: list[SiteAdapter], notifiers: list[Notifier], store, now: datetime | None = None, dry_run: bool = False) -> RunResult`
- Consumes: Task 1/4/5/6 的全部接口；`FetchError`、`NotifyError`、`StateError`

流水线规则（实现时逐条落实）：

1. `state = store.load()`；`now = now or datetime.now(timezone.utc)`；`dry_run=True` 时不发送、不保存。
2. 每个站点：`created = site_id not in state["sites"]`；`site_state = ensure_site(...)`；构造 `frozenset(site_state["seen"])` 传给 `adapter.fetch`。
3. 若 `created` 为真 → bootstrap：把所有 `item_id` 写入 seen；`config.bootstrap_notify` 为真时通过全部渠道 `send_text("监控已启动，当前 N 条商品")`；即使该文字消息失败也保存状态（记为 error），不计入 `new`。
4. 否则 `new_items = [l for l in listings if is_new(l, site_state)]`；再按 `matches` 过滤；按 `batch_size` 分批。
5. 对每批：所有渠道都尝试；任一成功即视为该批成功。**任一批全部失败 → 本次不标记该站点的任何 ID 为已见**，记 error。
6. 全部批次成功（或无需通知）后：把本次抓到的**全部** ID 写入 seen（包括被过滤掉的）。
7. 抓取失败：`consecutive_failures` 加 1（封顶为告警阈值）；达到阈值且超过告警节流 → 通过健康渠道 `send_text("[监控异常] ...")`；不改 seen。
8. 渠道发送失败：对应 `state["notifiers"][name]["consecutive_failures"]` 加 1（封顶同上）；成功归零；某渠道首次达到阈值时通过其它健康渠道告警（同样节流）。
9. `adapter.saturated` 为真且非 bootstrap → 通过健康渠道发一次「新增量异常」告警（节流）。
10. `prune_seen` 每个站点；若本次有实质变化（新增 seen、失败计数变化、锚点创建、告警时间变化）或 `should_commit` 为真 → `store.save(state)` 并更新 `last_commit_at`。只是 `last_run` 变化不算实质变化，避免每 5 分钟提交。
11. `store.save` 抛 `StateError` 时记入 `errors`，不向外抛。
12. 返回 `RunResult`；`used_browser` 为任一适配器本轮使用过浏览器。`dry_run` 时 `_send_*` 直接视为成功但不调用渠道。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_filters.py
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
```

```python
# tests/test_pipeline.py
from datetime import datetime, timezone, timedelta
from monitor.config import (Config, SiteConfig, Filters, NotifyConfig, AlertConfig)
from monitor.models import Listing
from monitor.pipeline import run_once
from monitor.state import new_state, FileStateStore
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
    name = "feishu"
    def __init__(self, fail=False): self.fail, self.items, self.texts = fail, [], []
    def send(self, items):
        if self.fail: raise NotifyError("x")
        self.items.extend(items)
    def send_text(self, text):
        if self.fail: raise NotifyError("x")
        self.texts.append(text)

class BrokenStore:
    def load(self): return new_state()
    def save(self, state): raise StateError("disk")

def cfg(bootstrap=True):
    return Config(sites={"jiaoyimao": SiteConfig("jiaoyimao", True, ("u",), 1, Filters())},
                  notify=NotifyConfig(("feishu",), 10),
                  alerts=AlertConfig(3, 180), bootstrap_notify=bootstrap)

def test_bootstrap_registers_without_item_push(tmp_path):
    store = FileStateStore(tmp_path / "s.json"); n = FakeNotifier()
    r = run_once(cfg(), [FakeAdapter([listing("1")])], [n], store, now=NOW)
    assert r.bootstrap is True and n.items == [] and len(n.texts) == 1
    assert "1" in store.load()["sites"]["jiaoyimao"]["seen"]

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
    bad, good = FakeNotifier(fail=True), FakeNotifier()
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
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_filters.py tests/test_pipeline.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `filters.py`：价格区间（`price is None` 且设置了价格边界视为不匹配）、平台/服务器精确匹配（配置为空表示不限）、关键词在 `title + description + sell_points` 中不区分大小写包含；`exclude_keywords` 命中即淘汰。
- `pipeline.py` 按上面 1–12 实现。`_send_items` / `_send_text_all` / `_send_alert` 三个内部函数负责渠道调用与计数；健康渠道 = `consecutive_failures == 0` 的渠道；没有健康渠道时跳过告警并记 error。告警节流用 `last_alert_at` 与 `min_alert_interval_minutes`。
- 失败计数封顶为阈值（`min(count + 1, threshold)`），避免连续故障时每轮都触发一次状态提交。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_filters.py tests/test_pipeline.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/filters.py monitor/pipeline.py tests/test_filters.py tests/test_pipeline.py
git commit -m "feat: dedup/filter/notify pipeline with failure semantics"
```

---

### Task 8: CLI、存活检测与 Actions workflow

**Files:**
- Create: `monitor/__main__.py`, `monitor/healthcheck.py`, `.github/workflows/monitor.yml`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int`
  - `ping(url: str | None) -> None`（失败只打印，不抛）
- Consumes: 所有前面的接口

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli.py
from monitor import __main__ as cli

def test_missing_config_exits_1(tmp_path):
    assert cli.main(["--config", str(tmp_path / "nope.yaml")], env={}) == 1

def test_dry_run_with_injected_adapter(monkeypatch, tmp_path):
    # dry-run 不要求 GITHUB_TOKEN / 通知密钥
    from monitor.models import Listing
    class A:
        site_id = "jiaoyimao"; used_browser = False; saturated = False
        def fetch(self, known_ids=frozenset()):
            return [Listing("jiaoyimao", "1", "t", 1, "u", None, {})]
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [A()])
    (tmp_path / "config.yaml").write_text(
        "sites:\n  jiaoyimao:\n    enabled: true\n    urls: [u]\n", encoding="utf-8")
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "--dry-run"],
                    env={"STATE_FILE": str(tmp_path / "s.json")}) == 0
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL（`monitor.__main__` 不存在）

- [ ] **Step 3: 实现**

- `main` 解析 `--config`（默认 `config.yaml`）与 `--dry-run`；读取 `env`（默认 `os.environ`）。
- 配置加载失败（文件不存在/`ValueError`）→ 打印错误，返回 1。
- dry-run：用 `FileStateStore(env.get("STATE_FILE", "state.local.json"))`、`notifiers=[]`，调用 `run_once(..., dry_run=True)`；打印 `RunResult` JSON 和「将要推送」的条目；返回 0（抓取错误也照常由 `run_once` 记录，不因 dry-run 返回非 0）。
- 正常运行：要求 `GITHUB_TOKEN` 与 `GITHUB_REPOSITORY`（缺失则打印并返回 1），构造 `GitHubStateStore(repo, token, branch=env.get("STATE_BRANCH", "state"))`；`build_notifiers` 从 `env` 读取密钥。
- 结束后：若 `env.get("GITHUB_STEP_SUMMARY")` 存在，把抓取/新增/通知/降级/错误写入该文件；错误为空时 `ping(env.get("HEALTHCHECK_PING_URL"))`；有错误返回 1，否则 0。
- `ping`：`requests.get(url, timeout=10)`，异常吞掉并 `print`。
- workflow 按 Global Constraints 写：`schedule` + `workflow_dispatch`、`permissions: contents: write`、`concurrency`、`timeout-minutes: 10`；`env` 注入全部 Secrets 与 `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`、`GITHUB_REPOSITORY: ${{ github.repository }}`、`STATE_BRANCH: state`；步骤：checkout、setup-python 3.12、`pip install -r requirements.txt`、`python -m monitor`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add monitor/__main__.py monitor/healthcheck.py .github/workflows/monitor.yml tests/test_cli.py
git commit -m "feat: CLI entrypoint, healthcheck ping and GitHub Actions workflow"
```

---

### Task 9: README 与部署验证（手动）

**Files:**
- Create: `README.md`
- Modify: none

**Interfaces:**
- Produces: 可照做的部署步骤

- [ ] **Step 1: 写 README**

内容必须包含：

1. 项目用途与架构一句话。
2. 本地测试：`pip install -r requirements.txt`、`python -m pytest tests -v`。
3. 本地试跑：`python -m monitor --config config.yaml --dry-run`。
4. 创建公开仓库并推送。
5. 创建 `state` 分支：

```bash
git checkout --orphan state
echo '{"version": 1, "sites": {}, "notifiers": {}}' > state.json
git add state.json && git commit -m "init state"
git push origin state
git checkout main
```

6. 配置 Secrets 表格：`FEISHU_WEBHOOK`（必填，若启用飞书）、`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`MAIL_TO`（若启用邮件）、`HEALTHCHECK_PING_URL`（可选）。
7. 飞书群机器人创建步骤（群设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook）。
8. 首次验证：Actions → monitor → Run workflow；预期收到「监控已启动，当前 N 条商品」。
9. 二次验证：在 `state` 分支从 `seen` 中删掉最新一条 goodsId，等下一轮，预期收到该商品推送，且之后不再重复。

- [ ] **Step 2: 提交**

```bash
git add README.md
git commit -m "docs: setup and deployment guide"
```

---

## 自检记录

- 设计文档 §6.1–§6.7、§7、§8、§9 均有对应任务：解析 Task 2、抓取/降级 Task 3、翻页 Task 4、状态 Task 5、通知 Task 6、流水线 Task 7、调度与运行 Task 8、初始化 Task 9。
- Review Focus 六条各自落在：1 → Task 3 `test_punish_response_raises`；2 → Task 2 fixture 测试；3 → Task 2 `test_empty_goods_list_is_valid` / `test_missing_initial_data_raises` + Task 4 把 `ValueError` 包成 `FetchError`；4 → Task 7 `test_notify_failure_does_not_mark_seen`；5 → Task 5 `test_github_store_retries_once_on_conflict` / `test_github_store_persistent_conflict_raises`；6 → Task 2 `test_invalid_goods_id_returns_none`。
- 接口命名跨任务一致：`fetch(known_ids)`、`send/send_text`、`load/save`、`run_once`、`ensure_site/ensure_notifier`。
- 与设计文档的少量实现层偏差已在对应任务中注明：`SiteAdapter.fetch` 增加 `known_ids` 提示参数（用于翻页提前停止）；`Notifier.alert` 统一为 `send_text`。
- 浏览器抓取本身不做自动化测试，由 Task 9 的 `workflow_dispatch` 集成验证覆盖。
