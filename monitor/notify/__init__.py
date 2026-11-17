"""Pluggable notification channels (Feishu webhook and email)."""

from __future__ import annotations

from typing import Mapping

from monitor.config import NotifyConfig
from monitor.errors import ConfigError
from monitor.notify.base import Notifier
from monitor.notify.email import EmailNotifier
from monitor.notify.feishu import FeishuNotifier

__all__ = ["Notifier", "FeishuNotifier", "EmailNotifier", "build_notifiers"]


def build_notifiers(cfg: NotifyConfig, env: Mapping[str, str]) -> list[Notifier]:
    """Instantiate the channels named by *cfg*, reading secrets from *env*."""
    notifiers: list[Notifier] = []
    for channel in cfg.channels:
        if channel == "feishu":
            app_id = env.get("FEISHU_APP_ID") or None
            app_secret = env.get("FEISHU_APP_SECRET") or None
            if bool(app_id) != bool(app_secret):
                raise ConfigError(
                    "FEISHU_APP_ID and FEISHU_APP_SECRET must be set together"
                )
            notifiers.append(
                FeishuNotifier(
                    _require(env, "FEISHU_WEBHOOK"),
                    app_id=app_id,
                    app_secret=app_secret,
                    include_images=cfg.include_images,
                    include_safe_report=cfg.include_safe_report,
                )
            )
        elif channel == "email":
            host = _require(env, "SMTP_HOST")
            port_raw = _require(env, "SMTP_PORT")
            user = _require(env, "SMTP_USER")
            password = _require(env, "SMTP_PASS")
            mail_to = _require(env, "MAIL_TO")
            try:
                port = int(port_raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"SMTP_PORT must be an integer, got {port_raw!r}"
                ) from exc
            notifiers.append(
                EmailNotifier(
                    host,
                    port,
                    user,
                    password,
                    mail_to,
                    use_ssl=port == 465,
                    include_images=cfg.include_images,
                    include_safe_report=cfg.include_safe_report,
                )
            )
        else:
            raise ConfigError(f"unknown notify channel: {channel}")
    return notifiers


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ConfigError(f"missing required environment variable: {key}")
    return value
