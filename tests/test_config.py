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
      require_account_transfer: true
notify:
  channels: [feishu, email]
  batch_size: 10
  include_images: false
  include_safe_report: false
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
    assert site.filters.require_account_transfer is True
    assert cfg.notify.channels == ("feishu", "email")
    assert cfg.notify.include_images is False
    assert cfg.notify.include_safe_report is False
    assert cfg.bootstrap_notify is True


def test_minimal_config_uses_defaults(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites:\n  jiaoyimao:\n    urls: [u]\n", encoding="utf-8")
    cfg = load_config(p)
    site = cfg.sites["jiaoyimao"]
    assert site.enabled is True and site.max_pages == 3
    assert site.filters == Filters()
    assert cfg.notify.batch_size == 10
    assert cfg.notify.include_images is True
    assert cfg.notify.include_safe_report is True
    assert cfg.alerts.consecutive_failures_before_alert == 3
    assert cfg.bootstrap_notify is True


def test_site_id_comes_from_yaml_key(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites:\n  jiaoyimao:\n    urls: [u]\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.sites["jiaoyimao"].site_id == "jiaoyimao"


def test_require_account_transfer_defaults_to_false(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites:\n  jiaoyimao:\n    urls: [u]\n", encoding="utf-8")
    assert load_config(p).sites["jiaoyimao"].filters.require_account_transfer is False


def test_require_account_transfer_must_be_boolean(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\n"
        "    filters:\n      require_account_transfer: 'yes'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    assert "require_account_transfer" in str(excinfo.value)


def test_include_images_must_be_boolean(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\n"
        "notify:\n  channels: [feishu]\n  include_images: 'no'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    assert "include_images" in str(excinfo.value)


def test_include_safe_report_must_be_boolean(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\n"
        "notify:\n  channels: [feishu]\n  include_safe_report: 'no'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    assert "include_safe_report" in str(excinfo.value)


def test_unknown_notify_channel_raises_value_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\nnotify:\n  channels: [telegram]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(p)


def test_malformed_yaml_raises_value_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("sites: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)


def test_empty_urls_raise_value_error_naming_site_and_field(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    message = str(excinfo.value)
    assert "jiaoyimao" in message and "urls" in message


def test_zero_max_pages_raises_value_error_naming_site_and_field(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sites:\n  jiaoyimao:\n    urls: [u]\n    max_pages: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    message = str(excinfo.value)
    assert "jiaoyimao" in message and "max_pages" in message
