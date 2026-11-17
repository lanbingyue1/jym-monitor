"""Command-line entrypoint: ``python -m monitor``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Mapping

from monitor.config import Config, load_config
from monitor.errors import ConfigError, FetchError, NotifyError, StateError
from monitor.filters import matches
from monitor.healthcheck import ping
from monitor.models import Listing
from monitor.notify import Notifier, build_notifiers
from monitor.pipeline import RunResult, run_once
from monitor.sites import build_adapters
from monitor.sites.base import SiteAdapter
from monitor.state import FileStateStore, GitHubStateStore

_DEFAULT_CONFIG = "config.yaml"
_DEFAULT_STATE_FILE = "state.local.json"
_DEFAULT_STATE_BRANCH = "state"


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the monitor once and return the process exit status."""
    environment = os.environ if env is None else env
    args = _build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 1

    if args.test_notify:
        return _test_notify(config, environment)
    if args.dry_run:
        return _dry_run(config, environment)
    return _real_run(config, environment)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m monitor",
        description="交易猫新品监控：抓取、过滤、通知并保存状态。",
    )
    parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"配置文件路径（默认 {_DEFAULT_CONFIG}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只抓取并打印将要推送的条目，不通知、不写状态",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="用筛选后最新一条商品给每个渠道发一条测试消息，不写状态",
    )
    return parser


def _dry_run(config: Config, env: Mapping[str, str]) -> int:
    """Fetch and report without notifying, writing state or touching secrets."""
    store = FileStateStore(env.get("STATE_FILE", _DEFAULT_STATE_FILE))
    try:
        adapters = build_adapters(config)
    except ConfigError as exc:
        print(f"站点配置失败: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_once(config, adapters, [], store, dry_run=True)
    except StateError as exc:
        print(f"状态读取失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(_counters(result), ensure_ascii=False))
    print(f"将要推送 {len(result.would_notify)} 条:")
    for item in result.would_notify:
        price = "价格未知" if item.price is None else f"¥{item.price}"
        print(f"{item.title} | {price} | {item.url}")
    return 0


_TEST_TEXT = "交易猫监控测试消息：收到这条说明该通知渠道配置正确。"
_TEST_SUBJECT = "交易猫监控测试消息"
# Detail pages are fetched one at a time; stop after this many candidates so a
# manually triggered test cannot run for minutes.
_SAMPLE_CANDIDATE_LIMIT = 10


def _test_notify(config: Config, env: Mapping[str, str]) -> int:
    """Send one message that shows what a real notification looks like.

    The message is built from the newest listing that passes the configured
    filters (and, with ``require_account_transfer``, whose detail page says
    「账号转移」= 是).  When nothing qualifies, a plain text message is sent so
    the channels can still be verified.  No state is read or written and
    ``GITHUB_TOKEN`` is not needed; every channel is attempted even when an
    earlier one fails.
    """
    try:
        notifiers = build_notifiers(config.notify, env)
    except ConfigError as exc:
        print(f"通知渠道配置失败: {exc}", file=sys.stderr)
        return 1
    if not notifiers:
        print("没有启用任何通知渠道", file=sys.stderr)
        return 1

    try:
        adapters = build_adapters(config)
    except ConfigError as exc:
        print(f"站点配置失败: {exc}", file=sys.stderr)
        return 1

    sample = _pick_sample(config, adapters)
    if sample is None:
        print("没有符合筛选条件的商品，改为发送文本测试消息", file=sys.stderr)
        return _send_text_test(notifiers)

    print(f"使用样例商品：{sample.title} | {_price_text(sample)} | {sample.url}")
    failed: list[str] = []
    for notifier in notifiers:
        try:
            notifier.send([sample])
        except NotifyError as exc:
            print(f"{notifier.name} 发送失败: {exc}", file=sys.stderr)
            failed.append(notifier.name)
        else:
            print(f"{notifier.name} 发送成功")
    return 1 if failed else 0


def _pick_sample(config: Config, adapters: list[SiteAdapter]) -> Listing | None:
    """Return the newest listing that passes the configured filters.

    Adapters return listings in site order, which is newest first.  Items are
    filtered with the same rules as a real run; when 「账号转移」 is required,
    detail pages are read until a matching item is found (bounded by
    ``_SAMPLE_CANDIDATE_LIMIT``).
    """
    by_site = {adapter.site_id: adapter for adapter in adapters}
    for site_id, site_cfg in config.sites.items():
        if not site_cfg.enabled:
            continue
        adapter = by_site.get(site_id)
        if adapter is None:
            continue
        try:
            listings = list(adapter.fetch(frozenset()))
        except FetchError as exc:
            print(f"{site_id}: 抓取失败: {exc}", file=sys.stderr)
            continue

        candidates = [item for item in listings if matches(item, site_cfg.filters)]
        if not candidates:
            continue
        if not site_cfg.filters.require_account_transfer:
            return candidates[0]

        fetch_detail = getattr(adapter, "fetch_detail", None)
        if fetch_detail is None:
            print(f"{site_id}: 适配器不支持详情页，「账号转移」无法筛选", file=sys.stderr)
            continue
        if len(candidates) > _SAMPLE_CANDIDATE_LIMIT:
            print(
                f"{site_id}: 只检查最新的 {_SAMPLE_CANDIDATE_LIMIT} 条候选商品",
                file=sys.stderr,
            )
        for item in candidates[:_SAMPLE_CANDIDATE_LIMIT]:
            try:
                detail = fetch_detail(item)
            except FetchError as exc:
                print(
                    f"{site_id}: 详情抓取失败 {item.item_id}: {exc}", file=sys.stderr
                )
                continue
            status = detail.safe_report.account_transfer if detail.safe_report else ""
            if status == "是":
                return detail
            print(f"{site_id}: 跳过 {item.item_id}（账号转移={status or '未知'}）")
    return None


def _send_text_test(notifiers: list[Notifier]) -> int:
    """Fallback: send a plain text test message through every channel."""
    failed: list[str] = []
    for notifier in notifiers:
        try:
            notifier.send_text(_TEST_TEXT, subject=_TEST_SUBJECT)
        except NotifyError as exc:
            print(f"{notifier.name} 发送失败: {exc}", file=sys.stderr)
            failed.append(notifier.name)
        else:
            print(f"{notifier.name} 发送成功")
    return 1 if failed else 0


def _price_text(item: Listing) -> str:
    return "价格未知" if item.price is None else f"¥{item.price}"


def _real_run(config: Config, env: Mapping[str, str]) -> int:
    """Execute a real run against the GitHub-backed state store."""
    missing = [
        name
        for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")
        if not env.get(name)
    ]
    if missing:
        print(f"缺少必需的环境变量: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        notifiers = build_notifiers(config.notify, env)
    except ConfigError as exc:
        print(f"通知渠道配置失败: {exc}", file=sys.stderr)
        return 1

    try:
        adapters = build_adapters(config)
    except ConfigError as exc:
        print(f"站点配置失败: {exc}", file=sys.stderr)
        return 1

    store = GitHubStateStore(
        env["GITHUB_REPOSITORY"],
        env["GITHUB_TOKEN"],
        branch=env.get("STATE_BRANCH", _DEFAULT_STATE_BRANCH),
    )

    started = time.monotonic()
    try:
        result = run_once(config, adapters, notifiers, store)
    except StateError as exc:
        message = f"状态读写失败: {exc}"
        print(message, file=sys.stderr)
        _write_summary(
            env.get("GITHUB_STEP_SUMMARY"),
            _render_failure_summary(message, time.monotonic() - started),
        )
        return 1
    elapsed = time.monotonic() - started

    _write_summary(env.get("GITHUB_STEP_SUMMARY"), _render_summary(result, elapsed))
    print(
        f"抓取 {result.fetched} · 新增 {result.new} · 通知 {result.notified}"
        f" · 告警 {result.alerts_sent} · 错误 {len(result.errors)}"
        f" · 警告 {len(result.warnings)}"
    )

    for warning in result.warnings:
        print(f"警告: {warning}", file=sys.stderr)

    if not result.errors:
        ping(env.get("HEALTHCHECK_PING_URL"))
    else:
        for error in result.errors:
            print(f"错误: {error}", file=sys.stderr)

    return 1 if result.errors else 0


def _counters(result: RunResult) -> dict[str, object]:
    """Return a JSON-safe view of *result* (``Listing`` objects excluded)."""
    return {
        "fetched": result.fetched,
        "new": result.new,
        "notified": result.notified,
        "bootstrap": result.bootstrap,
        "used_browser": result.used_browser,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "alerts_sent": result.alerts_sent,
    }


def _render_summary(result: RunResult, elapsed: float) -> str:
    lines = [
        "## 交易猫监控摘要",
        "",
        f"- 抓取：{result.fetched}",
        f"- 新增：{result.new}",
        f"- 通知：{result.notified}",
        f"- 告警：{result.alerts_sent}",
        f"- 降级（浏览器）：{'是' if result.used_browser else '否'}",
        f"- 首次运行：{'是' if result.bootstrap else '否'}",
        f"- 耗时：{elapsed:.2f}s",
        f"- 错误：{len(result.errors)}",
        f"- 警告：{len(result.warnings)}",
        "",
        "### 错误详情",
        "",
    ]
    if result.errors:
        lines.extend(f"- {error}" for error in result.errors)
    else:
        lines.append("- 无")
    if result.warnings:
        lines.extend(["", "### 警告详情", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def _render_failure_summary(message: str, elapsed: float) -> str:
    """Render a minimal summary for a run that failed before producing a result."""
    return (
        "## 交易猫监控摘要\n\n"
        "- 抓取：0\n"
        "- 新增：0\n"
        "- 通知：0\n"
        "- 降级（浏览器）：否\n"
        f"- 耗时：{elapsed:.2f}s\n"
        "- 错误：1\n\n"
        "### 错误详情\n\n"
        f"- {message}\n"
    )


def _write_summary(path: str | None, text: str) -> None:
    """Write the step summary when configured; a write failure is printed."""
    if not path:
        return
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"写入 GITHUB_STEP_SUMMARY 失败: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
