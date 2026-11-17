import pytest

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


def test_site_id_comes_from_yaml_key(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites:\n  jiaoyimao:\n    urls: [u]\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.sites["jiaoyimao"].site_id == "jiaoyimao"


def test_unknown_notify_channel_raises_value_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\nnotify:\n  channels: [telegram]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(p)
