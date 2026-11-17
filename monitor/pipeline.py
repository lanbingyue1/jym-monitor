"""Run-once pipeline: fetch, dedupe, filter, notify and persist state.

The pipeline is the only component that knows about failure semantics.  Its
contracts:

* ``store.load`` failures propagate to the caller; a failing ``store.save`` is
  recorded in ``RunResult.errors`` instead of being raised.
* A fetch failure increments the site's capped failure counter and may send one
  throttled ``[监控异常]`` alert through healthy channels.  Nothing is marked
  seen for that site.
* A notification batch succeeds when at least one channel accepts it.  When
  every channel fails for any batch, no ID from that site is marked seen, so
  the listings are retried on the next run (at-least-once).
* ``dry_run`` never calls a channel and never writes state.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from monitor.config import Config, SiteConfig
from monitor.errors import FetchError, NotifyError, StateError
from monitor.filters import matches
from monitor.models import Listing
from monitor.notify.base import Notifier
from monitor.sites.base import SiteAdapter
from monitor.state import (
    ensure_notifier,
    ensure_site,
    is_new,
    mark_seen,
    prune_seen,
    should_commit,
)

_SITE_ALERT_PREFIX = "[监控异常]"
_BOOTSTRAP_TEMPLATE = "监控已启动，当前 {count} 条商品"
_BOOTSTRAP_SUBJECT = "交易猫监控已启动"


@dataclass
class RunResult:
    """Summary of a single pipeline run."""

    fetched: int = 0
    new: int = 0
    notified: int = 0
    bootstrap: bool = False
    used_browser: bool = False
    errors: list[str] = field(default_factory=list)
    alerts_sent: int = 0
    would_notify: tuple[Listing, ...] = ()


def run_once(
    config: Config,
    adapters: list[SiteAdapter],
    notifiers: list[Notifier],
    store: Any,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RunResult:
    """Execute one monitoring pass and return its result."""
    state = store.load()
    moment = now or datetime.now(timezone.utc)
    state.setdefault("sites", {})
    state.setdefault("notifiers", {})

    runner = _Runner(config, list(adapters), list(notifiers), state, moment, dry_run)
    runner.process()

    if not dry_run:
        changed = runner.fingerprint_before != _fingerprint(state)
        if changed or should_commit(state, moment):
            state["last_commit_at"] = _format_ts(moment)
            try:
                store.save(state)
            except StateError as exc:
                runner.result.errors.append(f"state save failed: {exc}")
    return runner.result


class _Runner:
    def __init__(
        self,
        config: Config,
        adapters: list[SiteAdapter],
        notifiers: list[Notifier],
        state: dict[str, Any],
        now: datetime,
        dry_run: bool,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.notifiers = notifiers
        self.state = state
        self.now = now
        self.dry_run = dry_run
        self.result = RunResult()
        self.threshold = config.alerts.consecutive_failures_before_alert
        self.interval_minutes = config.alerts.min_alert_interval_minutes
        self.batch_size = max(1, config.notify.batch_size)
        self.fingerprint_before = _fingerprint(state)

    # -- orchestration ----------------------------------------------------

    def process(self) -> None:
        by_site: dict[str, SiteAdapter] = {}
        for adapter in self.adapters:
            by_site[adapter.site_id] = adapter

        for site_id, site_cfg in self.config.sites.items():
            if not site_cfg.enabled:
                continue
            adapter = by_site.get(site_id)
            if adapter is None:
                self.result.errors.append(f"{site_id}: enabled site has no adapter")
                continue
            try:
                self._process_site(site_id, site_cfg, adapter)
            except StateError as exc:
                # A corrupt value in one site's state must not abort the other
                # sites or discard the state already accumulated for this run;
                # the final save still persists counters notified earlier.
                self.result.errors.append(f"{site_id}: state error: {exc}")

    def _process_site(
        self, site_id: str, site_cfg: SiteConfig, adapter: SiteAdapter
    ) -> None:
        created = site_id not in self.state["sites"]
        site_state = ensure_site(self.state, site_id, self.now)
        known = frozenset(site_state["seen"])

        try:
            listings = list(adapter.fetch(known))
        except FetchError as exc:
            self._note_browser(adapter)
            self.result.errors.append(f"{site_id}: fetch failed: {exc}")
            self._record_site_failure(site_id, site_state)
            self._finish_site(site_state)
            return

        self._note_browser(adapter)
        self.result.fetched += len(listings)
        site_state["consecutive_failures"] = 0

        if created:
            # First run for this site: register the existing catalogue without
            # pushing individual listings.
            self.result.bootstrap = True
            mark_seen(site_state, listings, self.now)
            if self.config.bootstrap_notify:
                text = _BOOTSTRAP_TEMPLATE.format(count=len(listings))
                self._send_text_all(
                    text,
                    f"{site_id}: bootstrap notification failed",
                    subject=_BOOTSTRAP_SUBJECT,
                )
        else:
            self._notify_new(site_id, site_cfg, site_state, listings)

            if getattr(adapter, "saturated", False):
                self._site_alert(
                    site_id,
                    site_state,
                    f"{_SITE_ALERT_PREFIX} {site_id} 新增量异常："
                    "本次抓取达到翻页上限，可能存在漏报",
                )

        self._finish_site(site_state)

    def _finish_site(self, site_state: dict[str, Any]) -> None:
        prune_seen(site_state, self.now)
        site_state["last_run"] = _format_ts(self.now)

    def _note_browser(self, adapter: SiteAdapter) -> None:
        if getattr(adapter, "used_browser", False):
            self.result.used_browser = True

    def _notify_new(
        self,
        site_id: str,
        site_cfg: SiteConfig,
        site_state: dict[str, Any],
        listings: list[Listing],
    ) -> None:
        new_items = [item for item in listings if is_new(item, site_state)]
        actionable = [item for item in new_items if matches(item, site_cfg.filters)]
        self.result.new += len(actionable)
        self.result.would_notify += tuple(actionable)

        if not actionable:
            mark_seen(site_state, listings, self.now)
            return

        if not self.notifiers:
            if not self.dry_run:
                self.result.errors.append(
                    f"{site_id}: cannot notify {len(actionable)} new item(s): "
                    "no notification channels"
                )
                return
            mark_seen(site_state, listings, self.now)
            return

        failed = False
        for start in range(0, len(actionable), self.batch_size):
            batch = actionable[start : start + self.batch_size]
            if self._send_items(site_id, batch):
                self.result.notified += len(batch)
            else:
                failed = True

        if not failed:
            mark_seen(site_state, listings, self.now)

    # -- channel calls ----------------------------------------------------

    def _send_items(self, site_id: str, batch: list[Listing]) -> bool:
        if self.dry_run:
            return True

        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.send(batch)
            except NotifyError as exc:
                self.result.errors.append(
                    f"{site_id}: notifier {notifier.name} failed: {exc}"
                )
                self._record_channel_send(notifier.name, ok=False)
            else:
                self._record_channel_send(notifier.name, ok=True)
                delivered = True

        if not delivered:
            self.result.errors.append(
                f"{site_id}: notification failed for {len(batch)} item(s); "
                "they will be retried on the next run"
            )
        return delivered

    def _send_text_all(
        self, text: str, context: str, subject: str | None = None
    ) -> bool:
        if self.dry_run:
            return True
        if not self.notifiers:
            self.result.errors.append(f"{context}: no notification channels")
            return False

        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.send_text(text, subject=subject)
            except NotifyError as exc:
                self.result.errors.append(
                    f"{context}: notifier {notifier.name} failed: {exc}"
                )
                self._record_channel_send(notifier.name, ok=False)
            else:
                self._record_channel_send(notifier.name, ok=True)
                delivered = True
        return delivered

    # -- failure counters and alerts --------------------------------------

    def _record_site_failure(
        self, site_id: str, site_state: dict[str, Any]
    ) -> None:
        previous = int(site_state.get("consecutive_failures") or 0)
        current = min(previous + 1, self.threshold)
        site_state["consecutive_failures"] = current
        if previous < self.threshold <= current:
            self._site_alert(
                site_id,
                site_state,
                f"{_SITE_ALERT_PREFIX} {site_id} 连续抓取失败 {current} 次，"
                "请检查抓取链路",
            )

    def _record_channel_send(self, name: str, ok: bool) -> None:
        notifier_state = ensure_notifier(self.state, name)
        if ok:
            notifier_state["consecutive_failures"] = 0
            return

        previous = int(notifier_state.get("consecutive_failures") or 0)
        current = min(previous + 1, self.threshold)
        notifier_state["consecutive_failures"] = current
        if previous < self.threshold <= current:
            self._channel_alert(name, notifier_state, current)

    def _site_alert(
        self, site_id: str, site_state: dict[str, Any], text: str
    ) -> None:
        if _throttled(site_state.get("last_alert_at"), self.now, self.interval_minutes):
            return
        if self.dry_run:
            site_state["last_alert_at"] = _format_ts(self.now)
            self.result.alerts_sent += 1
            return

        healthy = self._healthy_notifiers(exclude=None)
        if not healthy:
            self.result.errors.append(
                f"{site_id}: alert suppressed: no healthy notification channel"
            )
            return

        delivered = False
        for notifier in healthy:
            try:
                notifier.send_text(text)
            except NotifyError as exc:
                self.result.errors.append(
                    f"{site_id}: alert via {notifier.name} failed: {exc}"
                )
                self._record_channel_send(notifier.name, ok=False)
            else:
                self._record_channel_send(notifier.name, ok=True)
                delivered = True

        if delivered:
            site_state["last_alert_at"] = _format_ts(self.now)
            self.result.alerts_sent += 1

    def _channel_alert(
        self, failed_name: str, notifier_state: dict[str, Any], count: int
    ) -> None:
        if _throttled(
            notifier_state.get("last_alert_at"), self.now, self.interval_minutes
        ):
            return
        if self.dry_run:
            notifier_state["last_alert_at"] = _format_ts(self.now)
            self.result.alerts_sent += 1
            return

        healthy = self._healthy_notifiers(exclude=failed_name)
        if not healthy:
            self.result.errors.append(
                f"notifier {failed_name}: alert suppressed: "
                "no healthy notification channel"
            )
            return

        text = (
            f"{_SITE_ALERT_PREFIX} 通知渠道 {failed_name} 连续失败 {count} 次，"
            "请检查通知配置"
        )
        delivered = False
        for notifier in healthy:
            try:
                notifier.send_text(text)
            except NotifyError as exc:
                self.result.errors.append(
                    f"notifier {failed_name}: alert via {notifier.name} failed: {exc}"
                )
                self._record_channel_send(notifier.name, ok=False)
            else:
                self._record_channel_send(notifier.name, ok=True)
                delivered = True

        if delivered:
            notifier_state["last_alert_at"] = _format_ts(self.now)
            self.result.alerts_sent += 1

    def _healthy_notifiers(self, exclude: str | None) -> list[Notifier]:
        healthy: list[Notifier] = []
        for notifier in self.notifiers:
            if exclude is not None and notifier.name == exclude:
                continue
            notifier_state = ensure_notifier(self.state, notifier.name)
            if int(notifier_state.get("consecutive_failures") or 0) == 0:
                healthy.append(notifier)
        return healthy


def _fingerprint(state: dict[str, Any]) -> str:
    """Return a deterministic digest of *state*.

    Per-site ``last_run`` and the timestamps inside ``seen`` are ignored: they
    are refreshed on every run and are not substantive changes.  ``seen`` is
    compared by its ID keys only, so pruning an entry (or registering a new ID)
    is still detected while a timestamp refresh alone does not force a commit.
    """
    clone = copy.deepcopy(state)
    sites = clone.get("sites")
    if isinstance(sites, dict):
        for site_state in sites.values():
            if not isinstance(site_state, dict):
                continue
            site_state.pop("last_run", None)
            seen = site_state.get("seen")
            if isinstance(seen, dict):
                site_state["seen"] = sorted(seen)
    return json.dumps(clone, sort_keys=True, ensure_ascii=False)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_ts(value: datetime) -> str:
    return _to_utc(value).isoformat()


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _to_utc(datetime.fromisoformat(text))


def _throttled(raw: Any, now: datetime, interval_minutes: int) -> bool:
    if raw is None:
        return False
    try:
        last = _parse_ts(raw)
    except (TypeError, ValueError) as exc:
        raise StateError(f"state has invalid last_alert_at: {raw!r}") from exc
    return _to_utc(now) - last < timedelta(minutes=interval_minutes)
