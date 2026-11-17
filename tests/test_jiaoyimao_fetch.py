import pytest
import requests
from monitor.errors import FetchError
from monitor.sites import jiaoyimao


class FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError()


def test_punish_response_raises(monkeypatch):
    monkeypatch.setattr(jiaoyimao.requests, "get",
                        lambda *a, **k: FakeResp('<script>x5secdata</script>'))
    monkeypatch.setattr(jiaoyimao.time, "sleep", lambda s: None)
    with pytest.raises(FetchError):
        jiaoyimao.fetch_html("https://example.com")


def test_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResp("x5secdata") if calls["n"] == 1 else FakeResp("<html>ok</html>")

    monkeypatch.setattr(jiaoyimao.requests, "get", fake_get)
    monkeypatch.setattr(jiaoyimao.time, "sleep", lambda s: None)
    assert jiaoyimao.fetch_html("https://example.com") == "<html>ok</html>"
    assert calls["n"] == 2


def test_non_2xx_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResp("server error", status=500) if calls["n"] == 1 else FakeResp("<html>ok</html>")

    monkeypatch.setattr(jiaoyimao.requests, "get", fake_get)
    monkeypatch.setattr(jiaoyimao.time, "sleep", sleeps.append)
    assert jiaoyimao.fetch_html("https://example.com") == "<html>ok</html>"
    assert calls["n"] == 2
    assert sleeps == [3]
