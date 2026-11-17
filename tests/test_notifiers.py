"""Tests for the Feishu and email notification channels."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone

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
# 2026-11-16 15:06:45 UTC == 2026-11-16 23:06 Beijing time.
PUBLISHED = datetime(2026, 11, 16, 15, 6, 45, tzinfo=timezone.utc)
PUBLISHED_BEIJING = "2026-11-16 23:06"


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
        self.sent_messages = []

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
        self.sent_messages.append(message)


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


def test_feishu_send_multiple_items_in_one_message(monkeypatch):
    calls = []
    other = Listing(
        "jiaoyimao",
        "5",
        "第二个商品",
        200,
        "https://x.test/5",
        None,
        {"server": "双平台", "platform": "iOS"},
    )

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([ITEM, other])

    assert len(calls) == 1
    elements = calls[0]["card"]["elements"]
    assert len(elements) == 2
    assert "6000勾玉 一命血影" in elements[0]["text"]["content"]
    assert "第二个商品" in elements[1]["text"]["content"]
    assert "¥200" in elements[1]["text"]["content"]


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


def test_feishu_send_text_ignores_subject(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send_text(
        "监控已启动，当前 16 条商品", subject="交易猫监控已启动"
    )

    assert sent["json"] == {
        "msg_type": "text",
        "content": {"text": "监控已启动，当前 16 条商品"},
    }


def test_feishu_card_includes_published_time_in_beijing(monkeypatch):
    sent = {}
    item = Listing(
        "jiaoyimao",
        "1794831310167680",
        "有时间商品",
        100,
        "https://x.test/1",
        PUBLISHED,
        {},
    )

    def fake_post(url, json=None, timeout=None):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([item])

    content = sent["json"]["card"]["elements"][0]["text"]["content"]
    assert f"上架时间：{PUBLISHED_BEIJING}" in content


def test_feishu_card_omits_published_time_when_unknown(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([ITEM])

    content = sent["json"]["card"]["elements"][0]["text"]["content"]
    assert "上架时间" not in content


def test_feishu_transport_error_does_not_leak_webhook(monkeypatch):
    token = "SECRET-WEBHOOK-TOKEN"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"

    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"failed to POST {webhook}")

    monkeypatch.setattr("monitor.notify.feishu.requests.post", boom)
    with pytest.raises(NotifyError) as excinfo:
        FeishuNotifier(webhook).send_text("x")

    text = str(excinfo.value)
    assert token not in text
    assert webhook not in text
    assert "ConnectionError" in text


def test_feishu_api_error_does_not_echo_response_message(monkeypatch):
    token = "SECRET-WEBHOOK-TOKEN"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"

    def fake_post(url, json=None, timeout=None):
        return FakeResponse(payload={"code": 19001, "msg": f"bad {webhook}"})

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    with pytest.raises(NotifyError) as excinfo:
        FeishuNotifier(webhook).send_text("x")

    text = str(excinfo.value)
    assert token not in text
    assert webhook not in text
    assert "19001" in text


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


def test_email_send_multiple_items_in_one_message(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")
    other = Listing(
        "jiaoyimao",
        "6",
        "第二个商品",
        200,
        "https://x.test/6",
        None,
        {"server": "双平台", "platform": "iOS"},
    )

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send(
        [ITEM, other]
    )

    assert len(created) == 1
    smtp = created[0]
    assert len(smtp.sent_messages) == 1
    assert smtp.message["Subject"] == "交易猫新品提醒 (2)"
    body = message_body(smtp.message)
    assert "6000勾玉 一命血影" in body
    assert "¥100" in body
    assert "第二个商品" in body
    assert "¥200" in body


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


def test_email_send_text_custom_subject(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send_text(
        "监控已启动，当前 16 条商品", subject="交易猫监控已启动"
    )

    assert created[0].message["Subject"] == "交易猫监控已启动"


def test_email_error_does_not_leak_recipients(monkeypatch):
    class BrokenSMTP(FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused(
                {
                    "a@example.com": (550, b"no such user"),
                    "b@example.com": (550, b"no such user"),
                }
            )

    monkeypatch.setattr("monitor.notify.email.smtplib.SMTP_SSL", BrokenSMTP)

    with pytest.raises(NotifyError) as excinfo:
        EmailNotifier(
            "smtp.qq.com", 465, "u", "p", "a@example.com, b@example.com"
        ).send_text("x")

    text = str(excinfo.value)
    assert "@" not in text
    assert "a@example.com" not in text
    assert "b@example.com" not in text
    assert "SMTPRecipientsRefused" in text


def test_email_body_includes_published_time_in_beijing(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")
    item = Listing(
        "jiaoyimao",
        "1794831310167680",
        "有时间商品",
        100,
        "https://x.test/1",
        PUBLISHED,
        {"server": "和之国", "platform": "安卓"},
    )

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([item])

    assert f"上架时间：{PUBLISHED_BEIJING}" in message_body(created[0].message)
    html_body = message_part(created[0].message, "html")
    assert "上架时间" in html_body and PUBLISHED_BEIJING in html_body


def test_email_body_omits_published_time_when_unknown(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([ITEM])

    assert "上架时间" not in message_body(created[0].message)
    assert "上架时间" not in message_part(created[0].message, "html")


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


# ---------------------------------------------------------------------------
# Rich listing content: images, account, seller note, safety report
# ---------------------------------------------------------------------------

from monitor.models import SafeReport

RICH = Listing(
    "jiaoyimao",
    "1794841605849819",
    "【极忍忍阶】血影，卫鲤",
    50,
    "https://www.jiaoyimao.com/jg1008016/1794841605849819.html",
    PUBLISHED,
    {
        "server": "和之国",
        "platform": "安卓",
        "account": "小米帐号",
        "description": "血影全皮，有通行证",
        "sell_points": "极忍",
    },
    ("https://img.test/1.jpg", "https://img.test/2.jpg"),
    SafeReport(
        "SSS",
        "100%",
        "100% 不会被找回",
        ("卖家信用认证：身份证认证 认证通过", "交易安全保障：账号转移 是"),
    ),
)


def test_feishu_card_includes_account_seller_note_and_report(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None, **kwargs):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([RICH])

    content = sent["json"]["card"]["elements"][0]["text"]["content"]
    assert "账号：小米帐号" in content
    assert "平台：安卓" in content
    assert "服务器：和之国" in content
    assert "特点：极忍" in content
    assert "卖家说：血影全皮，有通行证" in content
    assert "安全报告：SSS 100%（100% 不会被找回）" in content
    assert "卖家信用认证：身份证认证 认证通过" in content
    assert "交易安全保障：账号转移 是" in content


def test_feishu_renders_image_links_without_app_credentials(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None, **kwargs):
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)
    FeishuNotifier("https://hook").send([RICH])

    elements = sent["json"]["card"]["elements"]
    assert len(elements) == 2
    assert elements[1]["text"]["tag"] == "lark_md"
    content = elements[1]["text"]["content"]
    assert "[图1](https://img.test/1.jpg)" in content
    assert "[图2](https://img.test/2.jpg)" in content


class FakeDownload:
    def __init__(self, status_code=200, content=b"jpeg-bytes"):
        self.status_code = status_code
        self.content = content


def test_feishu_uploads_images_when_app_credentials_given(monkeypatch):
    posts = []
    downloads = []

    def fake_get(url, timeout=None):
        downloads.append(url)
        return FakeDownload()

    def fake_post(url, json=None, timeout=None, **kwargs):
        posts.append({"url": url, "json": json, **kwargs})
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={
                    "code": 0,
                    "tenant_access_token": "t-1",
                    "expire": 7200,
                }
            )
        if "im/v1/images" in url:
            key = f"img-{len([p for p in posts if 'im/v1/images' in p['url']])}"
            return FakeResponse(payload={"code": 0, "data": {"image_key": key}})
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    assert downloads == ["https://img.test/1.jpg", "https://img.test/2.jpg"]
    uploads = [p for p in posts if "im/v1/images" in p["url"]]
    assert len(uploads) == 2
    assert uploads[0]["headers"] == {"Authorization": "Bearer t-1"}
    assert uploads[0]["data"] == {"image_type": "message"}
    webhook = posts[-1]
    elements = webhook["json"]["card"]["elements"]
    images = [e for e in elements if e.get("tag") == "img"]
    assert [e["img_key"] for e in images] == ["img-1", "img-2"]
    assert all("text" not in e for e in images)


def test_feishu_retries_image_download_after_connect_timeout(monkeypatch):
    attempts = []
    sent = {}

    def flaky_get(url, timeout=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise requests.ConnectTimeout("connect timed out")
        return FakeDownload()

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(payload={"code": 0, "data": {"image_key": "img-1"}})
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", flaky_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    one = Listing(
        "jiaoyimao", "1", "单图", 1, "https://x.test/1", None, {},
        ("https://img.test/1.jpg",),
    )
    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([one])

    assert len(attempts) == 2
    images = [
        e for e in sent["json"]["card"]["elements"] if e.get("tag") == "img"
    ]
    assert [e["img_key"] for e in images] == ["img-1"]


def test_feishu_downloads_via_proxy_when_direct_times_out(monkeypatch, capsys):
    direct = []
    proxied = []
    sent = {}

    def fake_get(url, timeout=None):
        if "wsrv.nl" in url:
            proxied.append(url)
            return FakeDownload()
        direct.append(url)
        raise requests.ConnectTimeout("blocked")

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(payload={"code": 0, "data": {"image_key": "k"}})
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    # Direct download is attempted once per batch, then every image uses
    # the proxy instead of stalling on repeated timeouts.
    assert len(direct) == 3
    assert len(proxied) == 2
    assert all("%3A" in url for url in proxied)
    images = [
        e for e in sent["json"]["card"]["elements"] if e.get("tag") == "img"
    ]
    assert len(images) == 2
    assert "改用图片代理" in capsys.readouterr().err


def test_feishu_app_auth_policy_block_names_the_code_and_hint(monkeypatch, capsys):
    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(payload={"code": 21001, "msg": "denied"})
        return FakeResponse()

    def fake_get(url, timeout=None):
        return FakeDownload()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    err = capsys.readouterr().err
    assert "上传失败" in err
    assert "21001" in err
    assert "已发布" in err
    assert "denied" not in err  # the untrusted response message is never echoed


def test_feishu_strips_whitespace_from_app_credentials(monkeypatch):
    auth = {}

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            auth.update(json)
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(payload={"code": 0, "data": {"image_key": "k"}})
        return FakeResponse()

    def fake_get(url, timeout=None):
        return FakeDownload()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    one = Listing(
        "jiaoyimao", "1", "单图", 1, "https://x.test/1", None, {},
        ("https://img.test/1.jpg",),
    )
    FeishuNotifier(
        "https://hook", app_id=" cli_x\n", app_secret=" secret \n"
    ).send([one])

    assert auth == {"app_id": "cli_x", "app_secret": "secret"}


def test_feishu_upload_error_http_status_includes_feishu_code(monkeypatch, capsys):
    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(
                status_code=400, payload={"code": 99991672, "msg": "no permission"}
            )
        return FakeResponse()

    def fake_get(url, timeout=None):
        return FakeDownload()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    err = capsys.readouterr().err
    assert "HTTP 400 (code 99991672)" in err
    assert "no permission" not in err


def test_feishu_falls_back_to_links_when_upload_fails(monkeypatch, capsys):
    sent = {}

    def boom(url, timeout=None):
        raise requests.ConnectionError("down")

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", boom)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    elements = sent["json"]["card"]["elements"]
    assert [e for e in elements if e.get("tag") == "img"] == []
    assert "[图1](https://img.test/1.jpg)" in elements[1]["text"]["content"]
    assert "上传失败" in capsys.readouterr().err


def test_feishu_keeps_links_for_images_beyond_inline_limit(monkeypatch):
    sent = {}

    def fake_get(url, timeout=None):
        return FakeDownload()

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(payload={"code": 0, "data": {"image_key": "k"}})
        sent["json"] = json
        return FakeResponse()

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    many = Listing(
        "jiaoyimao", "9", "多图", 1, "https://x.test/9", None, {},
        tuple(f"https://img.test/{i}.jpg" for i in range(1, 12)),
    )
    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([many])

    elements = sent["json"]["card"]["elements"]
    assert len([e for e in elements if e.get("tag") == "img"]) == 9
    links = elements[-1]["text"]["content"]
    assert "[图10](https://img.test/10.jpg)" in links
    assert "[图11](https://img.test/11.jpg)" in links
    assert "[图1]" not in links


def test_build_feishu_notifier_rejects_partial_app_credentials():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    with pytest.raises(ConfigError):
        build_notifiers(
            cfg, {"FEISHU_WEBHOOK": "https://hook", "FEISHU_APP_ID": "cli_x"}
        )
    with pytest.raises(ConfigError):
        build_notifiers(
            cfg, {"FEISHU_WEBHOOK": "https://hook", "FEISHU_APP_SECRET": "s"}
        )


def test_build_feishu_notifier_accepts_app_credentials():
    cfg = NotifyConfig(channels=("feishu",), batch_size=10)
    notifiers = build_notifiers(
        cfg,
        {
            "FEISHU_WEBHOOK": "https://hook",
            "FEISHU_APP_ID": "cli_x",
            "FEISHU_APP_SECRET": "s",
        },
    )
    assert len(notifiers) == 1


def test_email_body_includes_account_note_report_and_images(monkeypatch):
    created = install_fake_smtp(monkeypatch, "SMTP_SSL")

    EmailNotifier("smtp.qq.com", 465, "u", "p", "me@example.com").send([RICH])

    body = message_body(created[0].message)
    assert "账号：小米帐号" in body
    assert "特点：极忍" in body
    assert "卖家说：血影全皮，有通行证" in body
    assert "安全报告：SSS 100%（100% 不会被找回）" in body
    assert "卖家信用认证：身份证认证 认证通过" in body
    assert "图片1：https://img.test/1.jpg" in body
    assert "图片2：https://img.test/2.jpg" in body

    html_body = message_part(created[0].message, "html")
    assert "<th>账号</th>" in html_body
    assert "小米帐号" in html_body
    assert 'href="https://img.test/1.jpg"' in html_body
    assert "卖家说：血影全皮，有通行证" in html_body


def test_feishu_retries_without_inline_images_when_card_rejected(
    monkeypatch, capsys
):
    posts = []

    def fake_get(url, timeout=None):
        return FakeDownload()

    def fake_post(url, json=None, timeout=None, **kwargs):
        if "tenant_access_token" in url:
            return FakeResponse(
                payload={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if "im/v1/images" in url:
            return FakeResponse(payload={"code": 0, "data": {"image_key": "bad"}})
        posts.append(json)
        has_img = any(e.get("tag") == "img" for e in json["card"]["elements"])
        return FakeResponse(
            payload={"code": 230001, "msg": "invalid image_key"} if has_img else None
        )

    monkeypatch.setattr("monitor.notify.feishu.requests.get", fake_get)
    monkeypatch.setattr("monitor.notify.feishu.requests.post", fake_post)

    FeishuNotifier(
        "https://hook", app_id="cli_x", app_secret="secret"
    ).send([RICH])

    assert len(posts) == 2
    assert any(e.get("tag") == "img" for e in posts[0]["card"]["elements"])
    retry = posts[1]["card"]["elements"]
    assert not any(e.get("tag") == "img" for e in retry)
    assert "[图1](https://img.test/1.jpg)" in retry[1]["text"]["content"]
    assert "改用链接重试" in capsys.readouterr().err
