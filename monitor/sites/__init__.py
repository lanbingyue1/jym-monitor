"""Site adapter registry.

Adding a site means adding a module under ``monitor/sites/`` and registering
its adapter class in ``_ADAPTER_FACTORIES``.
"""

from __future__ import annotations

from typing import Callable

from monitor.config import Config, SiteConfig
from monitor.errors import ConfigError
from monitor.sites.base import SiteAdapter
from monitor.sites.jiaoyimao import JiaoyimaoAdapter, SITE

_ADAPTER_FACTORIES: dict[str, Callable[[SiteConfig], SiteAdapter]] = {
    SITE: JiaoyimaoAdapter
}


def build_adapters(config: Config) -> list[SiteAdapter]:
    """Build one adapter for every enabled site in *config*.

    Raises ``ConfigError`` for a site that has no registered adapter.
    """
    adapters: list[SiteAdapter] = []
    for cfg in config.sites.values():
        factory = _ADAPTER_FACTORIES.get(cfg.site_id)
        if factory is None:
            raise ConfigError(f"unsupported site: {cfg.site_id}")
        if cfg.enabled:
            adapters.append(factory(cfg))
    return adapters
