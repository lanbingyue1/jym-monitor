"""Tests for the ``python -m monitor`` entrypoint and the liveness ping."""

import copy
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import yaml

from monitor import __main__ as cli
from monitor import healthcheck
from monitor.errors import ConfigError, FetchError, NotifyError, StateError
from monitor.models import Listing, SafeReport
from monitor.state import new_state

CONFIG = "sites:\n  jiaoyimao:\n    enabled: true\n    urls: [u]\n"
CONFIG_WITH_NOTIFY = CONFIG + "notify:\n  channels: [feishu]\n"
CONFIG_WITH_UNKNOWN_SITE = "sites:\n  other:\n    enabled: true\n    urls: [u]\n"

SEEDED_STATE = {
    "version": 1,
    "last_commit_at": None,
    "sites": {
        "jiaoyimao": {
            "anchor_at": "2026-01-01T00:00:00+00:00",
            "seen": {},
            "last_run": None,
            "consecutive_failures": 0,
            "last_alert_at": None,
        }
    },
    "notifiers": {},
}


class FakeAdapter:
    site_id = "jiaoyimao"
    used_browser = False
    saturated = False

    def __init__(self, items=()):
        self.items = list(items)

    def fetch(self, known_ids=frozenset()):
        return list(self.items)


class FailingAdapter(FakeAdapter):
    def fetch(self, known_ids=frozenset()):
        raise FetchError("blocked")


class FakeNotifier:
    name = "feishu"

    def __init__(self):
        self.items = []
        self.texts = []

    def send(self, items):
        self.items.extend(items)

    def send_text(self, text, *, subject=None):
        self.texts.append(text)


class MemoryStore:
    def __init__(self):
        self.state = new_state()

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)


class BrokenLoadStore:
    def load(self):
        raise StateError("disk")

    def save(self, state):
        raise AssertionError("must not save")


def write_config(tmp_path, text=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def output_of(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


def item(item_id="1", title="测试商品", price=180, url="https://x/1"):
    return Listing("jiaoyimao", item_id, title, price, url, None, {})


# ---------------------------------------------------------------------------
# Config and dry-run behaviour
# ---------------------------------------------------------------------------


def test_missing_config_exits_1(tmp_path):
    assert cli.main(["--config", str(tmp_path / "nope.yaml")], env={}) == 1


def test_invalid_config_exits_1(tmp_path):
    path = write_config(tmp_path, "sites: []\n")
    assert cli.main(["--config", str(path)], env={}) == 1


def test_module_entrypoint_runs_main(tmp_path):
    """``python -m monitor`` must execute main() and propagate its exit code."""
    proc = subprocess.run(
        [sys.executable, "-m", "monitor", "--config", str(tmp_path / "nope.yaml")],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode == 1
    assert "配置加载失败" in proc.stdout + proc.stderr


def test_dry_run_with_injected_adapter(monkeypatch, tmp_path):
    # dry-run does not require GITHUB_TOKEN or notification secrets
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([item()])])
    path = write_config(tmp_path)
    state_path = tmp_path / "s.json"
    assert (
        cli.main(
            ["--config", str(path), "--dry-run"],
            env={"STATE_FILE": str(state_path)},
        )
        == 0
    )
    assert not state_path.exists()


def test_dry_run_prints_would_notify_items(monkeypatch, tmp_path, capsys):
    items = [
        item(title="测试商品", price=180, url="https://x/1"),
        item("2", title="无价商品", price=None, url="https://x/2"),
    ]
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter(items)])
    path = write_config(tmp_path)
    state_path = tmp_path / "s.json"
    state_path.write_text(json.dumps(SEEDED_STATE), encoding="utf-8")
    before = state_path.read_text(encoding="utf-8")

    code = cli.main(
        ["--config", str(path), "--dry-run"], env={"STATE_FILE": str(state_path)}
    )

    output = output_of(capsys)
    assert code == 0
    assert "测试商品" in output and "https://x/1" in output
    assert "¥180" in output
    assert "无价商品" in output and "价格未知" in output
    assert '"new": 2' in output
    # dry-run never writes state
    assert state_path.read_text(encoding="utf-8") == before


def test_dry_run_corrupt_state_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [])
    path = write_config(tmp_path)
    state_path = tmp_path / "s.json"
    state_path.write_text("{not json", encoding="utf-8")

    code = cli.main(
        ["--config", str(path), "--dry-run"], env={"STATE_FILE": str(state_path)}
    )

    output = output_of(capsys)
    assert code == 1
    assert "状态读取失败" in output
    assert "invalid JSON" in output


def test_dry_run_unknown_site_exits_1(tmp_path, capsys):
    path = write_config(tmp_path, CONFIG_WITH_UNKNOWN_SITE)

    code = cli.main(
        ["--config", str(path), "--dry-run"],
        env={"STATE_FILE": str(tmp_path / "s.json")},
    )

    assert code == 1
    assert "unsupported site: other" in output_of(capsys)


def test_dry_run_ignores_errors_and_skips_summary_and_ping(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FailingAdapter()])
    pinged = []
    monkeypatch.setattr(cli, "ping", lambda url: pinged.append(url))
    path = write_config(tmp_path)
    summary = tmp_path / "summary.md"

    code = cli.main(
        ["--config", str(path), "--dry-run"],
        env={
            "STATE_FILE": str(tmp_path / "s.json"),
            "GITHUB_STEP_SUMMARY": str(summary),
            "HEALTHCHECK_PING_URL": "https://hc/1",
        },
    )

    output = output_of(capsys)
    assert code == 0
    assert "blocked" in output
    assert not summary.exists()
    assert pinged == []


# ---------------------------------------------------------------------------
# Real-run environment handling
# ---------------------------------------------------------------------------


def test_real_run_requires_token_and_repository(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter()])
    path = write_config(tmp_path)

    assert cli.main(["--config", str(path)], env={}) == 1
    assert "GITHUB_TOKEN" in output_of(capsys)

    assert cli.main(["--config", str(path)], env={"GITHUB_TOKEN": "t"}) == 1
    assert "GITHUB_REPOSITORY" in output_of(capsys)


def test_real_run_unknown_site_exits_1(tmp_path, capsys):
    path = write_config(tmp_path, CONFIG_WITH_UNKNOWN_SITE)

    code = cli.main(
        ["--config", str(path)],
        env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"},
    )

    assert code == 1
    assert "unsupported site: other" in output_of(capsys)


def test_notifier_config_error_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter()])

    def boom(cfg, env):
        raise ConfigError("missing required environment variable: FEISHU_WEBHOOK")

    monkeypatch.setattr(cli, "build_notifiers", boom)
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)

    code = cli.main(
        ["--config", str(path)],
        env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"},
    )

    assert code == 1
    assert "FEISHU_WEBHOOK" in output_of(capsys)


def test_state_load_failure_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [])
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [])
    monkeypatch.setattr(
        cli, "GitHubStateStore", lambda repo, token, branch="state": BrokenLoadStore()
    )
    path = write_config(tmp_path)

    code = cli.main(
        ["--config", str(path)],
        env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"},
    )

    assert code == 1
    assert "disk" in output_of(capsys)


# ---------------------------------------------------------------------------
# Real-run summary, ping and exit status
# ---------------------------------------------------------------------------


def test_state_load_failure_writes_failure_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [])
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [])
    monkeypatch.setattr(
        cli, "GitHubStateStore", lambda repo, token, branch="state": BrokenLoadStore()
    )
    pinged = []
    monkeypatch.setattr(cli, "ping", lambda url: pinged.append(url))
    path = write_config(tmp_path)
    summary = tmp_path / "summary.md"

    code = cli.main(
        ["--config", str(path)],
        env={
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_STEP_SUMMARY": str(summary),
            "HEALTHCHECK_PING_URL": "https://hc/1",
        },
    )

    assert code == 1
    text = summary.read_text(encoding="utf-8")
    assert "状态读写失败" in text and "disk" in text
    assert pinged == []


def test_real_run_writes_summary_and_pings(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([item()])])
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [FakeNotifier()])
    store_args = {}

    def fake_store(repo, token, branch="state"):
        store_args.update(repo=repo, token=token, branch=branch)
        return MemoryStore()

    monkeypatch.setattr(cli, "GitHubStateStore", fake_store)
    pinged = []
    monkeypatch.setattr(cli, "ping", lambda url: pinged.append(url))
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)
    summary = tmp_path / "summary.md"

    code = cli.main(
        ["--config", str(path)],
        env={
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_STEP_SUMMARY": str(summary),
            "HEALTHCHECK_PING_URL": "https://hc/1",
        },
    )

    assert code == 0
    assert store_args == {
        "repo": "owner/repo",
        "token": "token",
        "branch": "state",
    }
    text = summary.read_text(encoding="utf-8")
    assert "抓取" in text and "降级" in text and "错误" in text
    assert pinged == ["https://hc/1"]


def test_real_run_errors_write_summary_but_skip_ping(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FailingAdapter()])
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [FakeNotifier()])
    monkeypatch.setattr(
        cli, "GitHubStateStore", lambda repo, token, branch="state": MemoryStore()
    )
    pinged = []
    monkeypatch.setattr(cli, "ping", lambda url: pinged.append(url))
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)
    summary = tmp_path / "summary.md"

    code = cli.main(
        ["--config", str(path)],
        env={
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_STEP_SUMMARY": str(summary),
            "HEALTHCHECK_PING_URL": "https://hc/1",
        },
    )

    assert code == 1
    assert "blocked" in summary.read_text(encoding="utf-8")
    assert pinged == []


# ---------------------------------------------------------------------------
# ping()
# ---------------------------------------------------------------------------


def test_ping_none_makes_no_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        healthcheck.requests, "get", lambda *a, **k: calls.append((a, k))
    )
    assert healthcheck.ping(None) is None
    assert calls == []


def test_ping_calls_get_with_ten_second_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        healthcheck.requests,
        "get",
        lambda url, timeout=None: calls.append((url, timeout)),
    )
    assert healthcheck.ping("https://hc/1") is None
    assert calls == [("https://hc/1", 10)]


def test_ping_swallows_exceptions(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(healthcheck.requests, "get", boom)
    assert healthcheck.ping("https://hc/1") is None
    assert "network down" in output_of(capsys)


# ---------------------------------------------------------------------------
# Test notification
# ---------------------------------------------------------------------------

CONFIG_FILTERED = CONFIG + "    filters:\n      price_min: 100\n"
CONFIG_FILTERED_TRANSFER = (
    CONFIG_FILTERED + "      require_account_transfer: true\n"
)


class DetailAdapter(FakeAdapter):
    """Fake adapter whose detail pages report a configurable 「账号转移」."""

    def __init__(self, items=(), statuses=None):
        super().__init__(items)
        self.statuses = statuses or {}
        self.details = []

    def fetch_detail(self, listing):
        self.details.append(listing.item_id)
        status = self.statuses.get(listing.item_id, "")
        return replace(listing, safe_report=SafeReport(account_transfer=status))


class FailingNotifier(FakeNotifier):
    name = "email"

    def send(self, items):
        raise NotifyError("email delivery failed: SMTPAuthenticationError")

    def send_text(self, text, *, subject=None):
        raise NotifyError("email delivery failed: SMTPAuthenticationError")


def test_test_notify_sends_newest_matching_item(monkeypatch, tmp_path, capsys):
    newest = item("2", title="新商品", price=200, url="https://x/2")
    older = item("1", title="旧商品", price=200, url="https://x/1")
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([newest, older])])
    first, second = FakeNotifier(), FakeNotifier()
    second.name = "email"
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [first, second])
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)

    code = cli.main(["--config", str(path), "--test-notify"], env={})

    assert code == 0
    assert first.items == [newest] and second.items == [newest]
    assert "feishu 发送成功" in output_of(capsys)


def test_test_notify_applies_price_filter(monkeypatch, tmp_path):
    cheap = item("1", title="太便宜", price=50)
    good = item("2", title="符合价格", price=200)
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([cheap, good])])
    sent = FakeNotifier()
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [sent])
    path = write_config(tmp_path, CONFIG_FILTERED)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 0
    assert [entry.item_id for entry in sent.items] == ["2"]


def test_test_notify_requires_account_transfer(monkeypatch, tmp_path):
    no = item("1", title="不能转移", price=200)
    yes = item("2", title="可以转移", price=200)
    adapter = DetailAdapter([no, yes], {"1": "否", "2": "是"})
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [adapter])
    sent = FakeNotifier()
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [sent])
    path = write_config(tmp_path, CONFIG_FILTERED_TRANSFER)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 0
    assert [entry.item_id for entry in sent.items] == ["2"]
    assert sent.items[0].safe_report.account_transfer == "是"
    assert adapter.details == ["1", "2"]


def test_test_notify_falls_back_to_text_without_match(monkeypatch, tmp_path, capsys):
    cheap = item("1", title="太便宜", price=50)
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([cheap])])
    sent = FakeNotifier()
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [sent])
    path = write_config(tmp_path, CONFIG_FILTERED)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 0
    assert sent.items == []
    assert len(sent.texts) == 1
    assert "文本测试消息" in output_of(capsys)


def test_test_notify_fetch_failure_falls_back_to_text(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FailingAdapter()])
    sent = FakeNotifier()
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [sent])
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 0
    assert len(sent.texts) == 1


def test_test_notify_reports_failure_and_continues(monkeypatch, tmp_path, capsys):
    failing, ok = FailingNotifier(), FakeNotifier()
    ok.name = "feishu"
    monkeypatch.setattr(cli, "build_adapters", lambda cfg: [FakeAdapter([item()])])
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [failing, ok])
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)

    code = cli.main(["--config", str(path), "--test-notify"], env={})

    assert code == 1
    assert ok.items, "a failing channel must not stop the others"
    assert "email 发送失败" in output_of(capsys)


def test_test_notify_without_channels_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_notifiers", lambda cfg, env: [])
    path = write_config(tmp_path)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 1
    assert "没有启用任何通知渠道" in output_of(capsys)


def test_test_notify_config_error_exits_1(monkeypatch, tmp_path, capsys):
    def boom(cfg, env):
        raise ConfigError("missing required environment variable: SMTP_HOST")

    monkeypatch.setattr(cli, "build_notifiers", boom)
    path = write_config(tmp_path, CONFIG_WITH_NOTIFY)

    assert cli.main(["--config", str(path), "--test-notify"], env={}) == 1
    assert "SMTP_HOST" in output_of(capsys)


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


def test_workflow_definition():
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monitor.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["name"] == "monitor"
    assert data["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    assert "workflow_dispatch" in data["on"]
    assert data["permissions"] == {"contents": "write"}
    assert data["concurrency"] == {"group": "monitor", "cancel-in-progress": False}

    job = data["jobs"]["monitor"]
    assert job["timeout-minutes"] == 10
    env = job["env"]
    assert env["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert env["GITHUB_REPOSITORY"] == "${{ github.repository }}"
    assert env["STATE_BRANCH"] == "state"
    for name in (
        "FEISHU_WEBHOOK",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASS",
        "MAIL_TO",
        "HEALTHCHECK_PING_URL",
    ):
        assert env[name] == "${{ secrets.%s }}" % name

    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[1]["uses"].startswith("actions/setup-python@")
    assert steps[1]["with"]["python-version"] == "3.12"
    assert steps[2]["run"] == "pip install -r requirements.txt"
    assert steps[3]["run"] == "python -m monitor"
    assert steps[3]["if"] == "${{ !inputs.test_notify }}"
    assert steps[4]["run"] == "python -m monitor --test-notify"
    assert steps[4]["if"] == "${{ inputs.test_notify }}"

    dispatch = data["on"]["workflow_dispatch"]
    assert dispatch["inputs"]["test_notify"]["default"] is False
