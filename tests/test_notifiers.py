"""Tests for the Feishu and email notification channels."""

from __future__ import annotations

import smtplib

import pytest
import requests

from monitor.config import NotifyConfig
from monitor.errors import ConfigError, NotifyError
from monitor.models import Listing
from monitor.notify import build_notifiers
from monitor.notify.email import EmailNotifier
from monitor.notify.feishu import FeishuNotifier

ITEM = Listing(
    "jiaoyimao",
    "1794831310167680",
    "6000勾玉 一命血影",
    100,
    "https://www.jiaoyimao.com/jg1008016/1794831310167680.html",
    None,
    {"server": "和之国", "platform": "安卓"},
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = {"code": 0} if payload is None else payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.message = message


def install_fake_smtp(monkeypatch, attr_name):
    """Patch ``smtplib.<attr_name>`` and return the list of created instances."""
    created = []

    class RecordingSMTP(FakeSMTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(
        f"monitor.notify.email.smtplib.{attr_name}", RecordingSMTP
    )
    return created


def message_body(message):
    """Return the decoded text bodies of all text parts."""
    bodies = []
    for part in message.walk():
        if part.get_content_maintype() == "text":
            bodies.append(part.get_content())
    return "\n".join(bodies)


def message_part(message, subtype):
    """Return the decoded content of every text part with *subtype*."""
    return "\n".join(
        part.get_content()
        for part in message.walk()
        if part.get_content_maintype() == "text"
        and part.get_content_subtype() == subtype
    )


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------


def test_feishu_payload_contains_item(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"], sent["timeout"] = url, json, timeout
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook", timeout=5).send([ITEM])

    assert sent["url"] == "https://hook"
    assert sent["timeout"] == 5
    assert sent["json"]["msg_type"] == "interactive"
    element = sent["json"]["card"]["elements"][0]
    assert element["text"]["tag"] == "lark_md"
    content = element["text"]["content"]
    assert "6000勾玉 一命血影" in content
    assert "¥100" in content
    assert "和之国" in content
    assert "安卓" in content
    assert "https://www.jiaoyimao.com/jg1008016/1794831310167680.html" in content


def test_feishu_unknown_price(monkeypatch):
    sent = {}
    item = Listing("jiaoyimao", "2", "无价商品", None, "https://x.test/2", None, {})

    def fake_post(url, json=None, timeout=None):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([item])

    content = sent["json"]["card"]["elements"][0]["text"]["content"]
    assert "价格未知" in content
    assert "¥" not in content


def test_feishu_send_empty_items_is_noop(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([])

    assert calls == []


def test_feishu_send_text_payload(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send_text("[监控异常] 交易猫监控：抓取失败")

    assert sent["json"] == {
        "msg_type": "text",
        "content": {"text": "[监控异常] 交易猫监控：抓取失败"},
    }


def test_feishu_failure_raises(monkeypatch):
    monkeypatch.setattr(
        "monitor.notify.feishu.requests.post",
        lambda *a, **k: FakeResponse(status_code=500),
    )
    with pytest.raises(NotifyError):
        FeishuNotifier("https://hook").send_text("x")


def test_feishu_api_error_raises(monkeypatch):
    monkeypatch.setattr(
        "monitor.notify.feishu.requests.post",
        lambda *a, **k: FakeResponse(payload={"code": 19001, "msg": "bad"}),
    )
    with pytest.raises(NotifyError):
        FeishuNotifier("https://hook").send_text("x")


def test_feishu_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        "monitor.notify.feishu.requests.post",
        lambda *a, **k: FakeResponse(json_error=ValueError("not json")),
    )
    with pytest.raises(NotifyError):
        FeishuNotifier("https://hook").send_text("x")


def test_feishu_transport_error_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("monitor.notify.feishu.requests.post", boom)
    with pytest.raises(NotifyError):
        FeishuNotifier("https://hook").send_text("x")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def test_email_builds_message(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([ITEM])

    assert len(created) == 1
    smtp = created[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.qq.com", 465, 20)
    assert smtp.started_tls is False
    assert smtp.login_args == ("u", "p")
    assert smtp.message["Subject"] == "交易猫新品提醒 (1)"
    body = message_body(smtp.message)
    assert "6000勾玉 一命血影" in body
    assert "¥100" in body


def test_email_escapes_fields_and_links_http_urls(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")
    item = Listing(
        "jiaoyimao",
        "3",
        "<b>危险标题</b>",
        250,
        "https://x.test/item?a=1&b=2",
        None,
        {"server": "a&b", "platform": "<ios>"},
    )

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([item])

    body = message_part(created[0].message, "html")
    assert "<b>危险标题</b>" not in body
    assert "&lt;b&gt;危险标题&lt;/b&gt;" in body
    assert 'href="https://x.test/item?a=1&amp;b=2"' in body
    assert "a&amp;b" in body
    assert "&lt;ios&gt;" in body
    assert "¥250" in body


def test_email_non_http_url_is_not_linked(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")
    item = Listing(
        "jiaoyimao",
        "4",
        "标题",
        100,
        "javascript:alert(1)",
        None,
        {},
    )

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([item])

    body = message_part(created[0].message, "html")
    assert "<a " not in body
    assert "javascript:alert(1)" in body


def test_email_send_text(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send_text(
        "抓取失败：超时"
    )

    message = created[0].message
    assert message["Subject"] == "[监控异常] 交易猫监控"
    assert message_body(message).strip() == "抓取失败：超时"


def test_email_send_empty_items_is_noop(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([])

    assert created == []


def test_email_uses_starttls_when_not_ssl(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP")

    EmailNotifier(
        "smtp.qq.com", 587, "u", "p", "me@example.com", use_ssl=False
    ).send_text("x")

    assert len(created) == 1
    assert created[0].started_tls is True
    assert created[0].timeout == 20


def test_email_splits_recipients(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier(
        "smtp.qq.com",
        465,
        "u",
        "p",
        " a@example.com, ,b@example.com ",
    ).send_text("x")

    assert created[0].message["To"] == "a@example.com, b@example.com"


def test_email_smtp_error_raises(monkeypatch):
    class BrokenSMTP(FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr("monitor.notify.email.smtplib.SMTP_SSL", BrokenSMTP)

    with pytest.raises(NotifyError):
        EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send_text("x")


def test_email_connection_error_raises(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("monitor.notify.email.smtplib.SMTP_SSL", refuse)

    with pytest.raises(NotifyError):
        EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send_text("x")


# ---------------------------------------------------------------------------
# build_notifiers
# ---------------------------------------------------------------------------

EMAIL_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "465",
    "SMTP_USER": "user@example.com",
    "SMTP_PASS": "secret",
    "MAIL_TO": "me@example.com,other@example.com",
}


def test_build_notifiers_requires_env():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    with pytest.raises(ConfigError):
        build_notifiers(cfg, {})


def test_build_notifiers_no_channels():
    assert build_notifiers(NotifyConfig(channels=(), batch_size=10), {}) == []


def test_build_feishu_notifier():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    notifiers = build_notifiers(cfg, {"FEISHU_WEBHOOK": "https://hook"})
    assert [n.name for n in notifiers] == ["feishu"]
    assert isinstance(notifiers[0], FeishuNotifier)


def test_build_email_notifier_ssl_port():
    cfg = NotifyConfig(channels=("email",), batch_size=10)

    notifiers = build_notifiers(cfg, EMAIL_ENV)

    assert [n.name for n in notifiers] == ["email"]
    notifier = notifiers[0]
    assert isinstance(notifier, EmailNotifier)
    assert (notifier.host, notifier.port) == ("smtp.example.com", 465)
    assert notifier.use_ssl is True
    assert notifier.to == ["me@example.com", "other@example.com"]


def test_build_email_notifier_starttls_port():
    cfg = NotifyConfig(channels=("email",), batch_size=10)

    notifier = build_notifiers(cfg, {**EMAIL_ENV, "SMTP_PORT": "587"})[0]

    assert notifier.port == 587
    assert notifier.use_ssl is False


def test_build_email_notifier_rejects_non_numeric_port():
    cfg = NotifyConfig(channels=("email",), batch_size=10)
    with pytest.raises(ConfigError):
        build_notifiers(cfg, {**EMAIL_ENV, "SMTP_PORT": "imap"})


@pytest.mark.parametrize(
    "missing", ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
)
def test_build_email_notifier_requires_each_env(missing):
    cfg = NotifyConfig(channels=("email",), batch_size=10)
    env = {k: v for k, v in EMAIL_ENV.items() if k != missing}
    with pytest.raises(ConfigError):
        build_notifiers(cfg, env)


def test_build_notifiers_preserves_channel_order():
    cfg = NotifyConfig(channels=("email", "feishu"), batch_size=10)
    env = {**EMAIL_ENV, "FEISHU_WEBHOOK": "https://hook"}

    notifiers = build_notifiers(cfg, env)

    assert [n.name for n in notifiers] == ["email", "feishu"]


def test_build_notifiers_rejects_unknown_channel():
    cfg = NotifyConfig(channels=("slack",), batch_size=10)
    with pytest.raises(ConfigError):
        build_notifiers(cfg, {})
