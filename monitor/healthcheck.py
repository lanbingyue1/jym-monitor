"""Optional liveness signal for external healthcheck services."""

from __future__ import annotations

import sys

import requests

_PING_TIMEOUT_SECONDS = 10


def ping(url: str | None) -> None:
    """Ping *url* once; any failure is printed and never raised."""
    if url is None:
        return
    try:
        requests.get(url, timeout=_PING_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a ping must never fail the run
        print(f"存活检测 ping 失败: {exc}", file=sys.stderr)
