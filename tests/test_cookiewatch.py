"""cookiewatch 每日主动探测的离线测试（注入 fake check/notify，无网络）。"""
from __future__ import annotations

import requests

from app.cookiewatch import probe
from app.token_api import TokenFetchError


class _NotifyRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, title, text=""):
        self.calls.append((kind, title, text))


def _run(check, notify):
    return probe("ck", "unb", check=check, notify_fn=notify)


def test_success_no_alert():
    n = _NotifyRecorder()
    rc = _run(lambda ck, mid: "tok", n)
    assert rc == 0 and n.calls == []


def test_session_expired_alerts_and_exit_1():
    n = _NotifyRecorder()

    def boom(ck, mid):
        raise TokenFetchError("token 接口返回失败: ... FAIL_SYS_SESSION_EXPIRED::Session过期")

    assert _run(boom, n) == 1
    assert len(n.calls) == 1
    kind, title, text = n.calls[0]
    assert kind == "cookie_expired"
    assert "失效" in title and "每日检测" in title
    assert "Session过期" in text


def test_rgv587_risk_alerts():
    n = _NotifyRecorder()

    def boom(ck, mid):
        raise TokenFetchError("触发风控/限流（RGV587_ERROR）: ... 被挤爆啦")

    assert _run(boom, n) == 1
    assert "风控" in n.calls[0][1]


def test_network_error_is_not_false_positive():
    n = _NotifyRecorder()

    def down(ck, mid):
        raise requests.ConnectionError("连接失败")

    # 网络抖动：不当失效，不告警，退出码 0（明日 timer 再试）
    assert _run(down, n) == 0
    assert n.calls == []


def test_no_cookie_returns_2():
    n = _NotifyRecorder()
    assert probe("", "unb", check=lambda ck, mid: "tok", notify_fn=n) == 2
    assert n.calls == []
