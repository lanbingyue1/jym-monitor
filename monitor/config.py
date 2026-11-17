"""Configuration models and YAML loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

KNOWN_NOTIFY_CHANNELS = ("feishu", "email")


@dataclass(frozen=True)
class Filters:
    price_min: int | None = None
    price_max: int | None = None
    platforms: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    # Only notify listings whose detail page reports 「账号转移」= 是.  This
    # needs the detail page, so the pipeline applies it after enrichment.
    require_account_transfer: bool = False


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    enabled: bool
    urls: tuple[str, ...]
    max_pages: int
    filters: Filters


@dataclass(frozen=True)
class NotifyConfig:
    channels: tuple[str, ...] = ()
    batch_size: int = 10


@dataclass(frozen=True)
class AlertConfig:
    consecutive_failures_before_alert: int = 3
    min_alert_interval_minutes: int = 180


@dataclass(frozen=True)
class Config:
    sites: Mapping[str, SiteConfig]
    notify: NotifyConfig = NotifyConfig()
    alerts: AlertConfig = AlertConfig()
    bootstrap_notify: bool = True


def load_config(path: str | Path) -> Config:
    """Load and validate the YAML config file at *path*."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

    sites_raw = raw.get("sites")
    if not isinstance(sites_raw, dict) or not sites_raw:
        raise ValueError("config must define at least one site under 'sites'")

    sites = {
        str(site_id): _parse_site(str(site_id), site_raw)
        for site_id, site_raw in sites_raw.items()
    }

    notify_raw = _section(raw, "notify")
    channels = _as_tuple(notify_raw.get("channels"), "notify.channels")
    unknown = [c for c in channels if c not in KNOWN_NOTIFY_CHANNELS]
    if unknown:
        raise ValueError(
            f"unknown notify channel(s): {', '.join(map(str, unknown))}"
        )

    alerts_raw = _section(raw, "alerts")
    bootstrap_raw = _section(raw, "bootstrap")

    return Config(
        sites=sites,
        notify=NotifyConfig(
            channels=channels,
            batch_size=notify_raw.get("batch_size", 10),
        ),
        alerts=AlertConfig(
            consecutive_failures_before_alert=alerts_raw.get(
                "consecutive_failures_before_alert", 3
            ),
            min_alert_interval_minutes=alerts_raw.get(
                "min_alert_interval_minutes", 180
            ),
        ),
        bootstrap_notify=bootstrap_raw.get("notify_on_first_run", True),
    )


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"'{name}' must be a mapping")
    return value


def _parse_site(site_id: str, raw: Any) -> SiteConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"site '{site_id}' must be a mapping")
    urls = _as_tuple(raw.get("urls"), f"sites.{site_id}.urls")
    if not urls:
        raise ValueError(f"site '{site_id}' field 'urls' must not be empty")

    max_pages = raw.get("max_pages", 3)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int):
        raise ValueError(
            f"site '{site_id}' field 'max_pages' must be a positive integer"
        )
    if max_pages <= 0:
        raise ValueError(
            f"site '{site_id}' field 'max_pages' must be a positive integer, "
            f"got {max_pages}"
        )

    filters_raw = _section(raw, "filters")
    return SiteConfig(
        site_id=site_id,
        enabled=raw.get("enabled", True),
        urls=urls,
        max_pages=max_pages,
        filters=Filters(
            price_min=filters_raw.get("price_min"),
            price_max=filters_raw.get("price_max"),
            platforms=_as_tuple(
                filters_raw.get("platforms"), f"sites.{site_id}.filters.platforms"
            ),
            servers=_as_tuple(
                filters_raw.get("servers"), f"sites.{site_id}.filters.servers"
            ),
            include_keywords=_as_tuple(
                filters_raw.get("include_keywords"),
                f"sites.{site_id}.filters.include_keywords",
            ),
            exclude_keywords=_as_tuple(
                filters_raw.get("exclude_keywords"),
                f"sites.{site_id}.filters.exclude_keywords",
            ),
            require_account_transfer=_as_bool(
                filters_raw.get("require_account_transfer"),
                f"sites.{site_id}.filters.require_account_transfer",
            ),
        ),
    )


def _as_bool(value: Any, name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"'{name}' must be true or false")
    return value


def _as_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"'{name}' must be a list")
    return tuple(value)
