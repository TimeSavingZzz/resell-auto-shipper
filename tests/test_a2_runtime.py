"""A2 新增能力离线测试：告警节流、cookie 热刷新、商品 reload/enabled。

不触网、不改动真实 .env / data / config：全部用 tmp_path 隔离，
client 的全局 config 用假对象替换以避免写真实 .env。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.alerter import Alerter
from app import config as config_mod
from app.config import Config
from app.catalog import Catalog
import app.client as client_mod
from app.client import ListenerClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- config：myid / set_cookie ----

def test_config_myid_reads_unb_from_cookie(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / ".env")
    c = Config()
    c.set_cookie("tracknick=x; unb=2961111111; cookie2=abc")
    assert c.myid == "2961111111"


def test_config_set_cookie_updates_memory_and_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.delenv("COOKIES_STR", raising=False)
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "ENV_PATH", env)
    c = Config()
    assert not c.has_auth
    c.set_cookie("unb=12345")
    assert c.cookies_str == "unb=12345"
    assert c.has_auth and c.auth_ok
    content = env.read_text(encoding="utf-8")
    assert content.startswith("COOKIES_STR=")
    assert "unb=12345" in content  # dotenv 可能给含 = 的值加引号


# ---- catalog：enabled / reload / 历史统计含停用商品 ----

def _write_json(path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _catalog(tmp_path, data, db=":memory:"):
    products = tmp_path / "products.json"
    _write_json(products, data)
    return Catalog(products_path=products, db_path=db, min_online=2)


def test_enabled_disabled_resolve_lookup(tmp_path):
    data = {
        "products": {
            "pA": {"name": "A", "message": "话术", "source_item_id": "100"},
            "pB": {"name": "B", "message": "话术", "source_item_id": "200", "enabled": False},
        }
    }
    cat = _catalog(tmp_path, data)
    assert cat.resolve("100") is not None
    assert cat.resolve("200") is None            # 停用：自动发货/补货不命中
    assert cat._lookup("200")["key"] == "pB"     # 历史统计仍可归属
    assert "100" in cat.item_map() and "200" not in cat.item_map()
    assert cat.active_product_keys() == ["pA"]
    cat.close()


def test_reload_toggles_and_adds_product(tmp_path):
    products = tmp_path / "products.json"
    _write_json(
        products,
        {"products": {"pA": {"name": "A", "message": "m", "source_item_id": "100"}}},
    )
    cat = Catalog(products_path=products, db_path=str(tmp_path / "relist.db"), min_online=2)
    cat.register_alias("5550000000000", "pA")
    assert cat.resolve("100") is not None

    # 停用 pA + 新增 pC
    _write_json(
        products,
        {"products": {
            "pA": {"name": "A", "message": "m", "source_item_id": "100", "enabled": False},
            "pC": {"name": "C", "message": "m", "source_item_id": "300"},
        }},
    )
    cat.reload()
    assert cat.resolve("100") is None
    assert cat.resolve("300") is not None
    assert cat.active_product_keys() == ["pC"]
    # 既有别名保留（DB 幂等），停用商品从 item_map 排除
    assert "5550000000000" in cat.alias_ids("pA")
    assert cat.resolve("5550000000000") is None
    cat.close()


def test_counts_include_disabled_history(tmp_path):
    data = {
        "products": {
            "pA": {"name": "A", "message": "m", "source_item_id": "100"},
            "pB": {"name": "B", "message": "m", "source_item_id": "200", "enabled": False},
        }
    }
    cat = _catalog(tmp_path, data)
    db = tmp_path / "deliveries.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE deliveries (item_id TEXT)")
    conn.executemany("INSERT INTO deliveries (item_id) VALUES (?)", [("100",), ("200",), ("999",)])
    conn.commit()
    conn.close()
    counts = cat.counts_by_product(db)
    assert counts["pA"] == 1
    assert counts["pB"] == 1  # 停用商品的历史成交仍计入报表
    cat.close()


# ---- alerter：节流 ----

def test_alerter_throttles_same_kind(monkeypatch):
    pushed = []
    a = Alerter(channel="serverchan", sendkey="x", throttle=1000)
    monkeypatch.setattr(a, "_push_serverchan", lambda title, text: pushed.append((title, text)))
    a.notify("cookie_expired", "第一次")
    a.notify("cookie_expired", "第二次应被抑制")
    assert len(pushed) == 1 and pushed[0][0] == "第一次"
    a.notify("delivery_failed", "不同 kind 不受影响")
    assert len(pushed) == 2


def test_alerter_log_channel_is_safe():
    a = Alerter(channel="log")
    a.notify("cookie_expired", "仅日志，不抛异常")  # 默认不打外推
    assert a.enabled is True


# ---- alerter：邮件附加渠道 ----

def _email_alerter(**kw):
    base = dict(channel="serverchan", sendkey="x", email_to="dj@sdust.edu.cn",
                smtp_user="send@qq.com", smtp_pass="authcode", smtp_host="smtp.qq.com")
    base.update(kw)
    return Alerter(**base)


def test_alerter_email_is_parallel_to_main_channel(monkeypatch):
    a = _email_alerter()
    calls = []
    monkeypatch.setattr(a, "_push_serverchan", lambda t, x: calls.append(("sc", t)))
    monkeypatch.setattr(a, "_push_email", lambda t, x: calls.append(("em", t)))
    a.notify("x", "标题")
    assert calls == [("sc", "标题"), ("em", "标题")]


def test_alerter_email_needs_full_smtp_config(monkeypatch):
    a = Alerter(channel="serverchan", sendkey="x")  # 缺 email_to / smtp → 不启邮件
    calls = []
    monkeypatch.setattr(a, "_push_email", lambda t, x: calls.append(t))
    a.notify("x", "标题")
    assert calls == []


def test_alerter_log_with_email_still_pushes_email(monkeypatch):
    a = Alerter(channel="log", email_to="dj@sdust.edu.cn",
                smtp_user="send@qq.com", smtp_pass="authcode")
    calls = []
    monkeypatch.setattr(a, "_push_email", lambda t, x: calls.append(t))
    a.notify("x", "标题")  # log 主渠道照常记日志，邮件作为附加照发
    assert calls == ["标题"]


def test_alerter_throttle_covers_all_targets(monkeypatch):
    a = _email_alerter(throttle=1000)
    calls = []
    monkeypatch.setattr(a, "_push_serverchan", lambda t, x: calls.append(("sc", t)))
    monkeypatch.setattr(a, "_push_email", lambda t, x: calls.append(("em", t)))
    a.notify("k", "一")
    a.notify("k", "二应抑制")
    assert calls == [("sc", "一"), ("em", "一")]


def test_alerter_email_send_via_smtp(monkeypatch):
    import sys
    import types

    calls = {"login": None, "msgs": []}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            calls["login"] = (u, p)

        def sendmail(self, frm, to, msg):
            calls["msgs"].append((frm, to, msg))

        def quit(self):
            pass

    fake = types.ModuleType("smtplib")
    fake.SMTP_SSL = lambda *a, **k: _FakeClient()
    monkeypatch.setitem(sys.modules, "smtplib", fake)

    a = _email_alerter(channel="log")  # log 主渠道 → 只走 email 推送，离线安全
    a.notify("k", "测试主题", "正文内容")
    assert calls["login"] == ("send@qq.com", "authcode")
    assert calls["msgs"]
    frm, to, msg = calls["msgs"][0]
    assert to == ["dj@sdust.edu.cn"]
    assert "send@qq.com" in frm
    assert "To: dj@sdust.edu.cn" in msg and "Subject: =?utf-8" in msg  # 主题按 UTF-8 base64 编码
    # 中文正文经 base64 传输，解码后应能还原
    import base64

    _, payload = msg.split("\n\n", 1)
    assert "正文内容" in base64.b64decode(payload.strip()).decode("utf-8")


# ---- client：control.json 热刷新 ----

class _FakeConfig:
    base_url = "wss://example.invalid/"
    heartbeat_interval = 15
    heartbeat_timeout = 30
    max_backoff = 60

    def __init__(self):
        self.set_calls = []

    @property
    def myid(self):
        return ""

    def set_cookie(self, new_cookie: str):
        self.set_calls.append(new_cookie)


def _fresh_client(monkeypatch, tmp_path, cookies="unb=111; cna=old"):
    monkeypatch.delenv("AUTO_SHIP", raising=False)
    monkeypatch.delenv("AUTO_RELIST", raising=False)
    fake = _FakeConfig()
    monkeypatch.setattr(client_mod, "config", fake)
    client = ListenerClient(cookies)
    client._control_path = tmp_path / "control.json"
    client._control_last_cookie = None
    client._control_last_products = None
    return client, fake


def test_client_apply_cookie_refreshes_snapshots(monkeypatch, tmp_path):
    client, fake = _fresh_client(monkeypatch, tmp_path)
    (tmp_path / "control.json").write_text(
        json.dumps({"cookie": "unb=222; cna=new"}), encoding="utf-8"
    )
    assert client.cookies_str == "unb=111; cna=old"
    _run(client._maybe_apply_control())
    assert client.cookies_str == "unb=222; cna=new"
    assert fake.set_calls == ["unb=222; cna=new"]
    assert client._cookie_refreshed.is_set()

    # 同值再轮询：不重复 set_cookie
    _run(client._maybe_apply_control())
    assert fake.set_calls == ["unb=222; cna=new"]


def test_client_products_version_triggers_catalog_reload(monkeypatch, tmp_path):
    client, _fake = _fresh_client(monkeypatch, tmp_path)
    products = tmp_path / "products.json"
    _write_json(products, {"products": {"pA": {"name": "A", "message": "m", "source_item_id": "100"}}})
    cat = Catalog(products_path=products, db_path=str(tmp_path / "relist.db"), min_online=2)
    client.catalog = cat

    # cookie 与启动值一致 → 不触发 cookie 分支
    (tmp_path / "control.json").write_text(
        json.dumps({"cookie": "unb=111; cna=old", "products_version": 1}), encoding="utf-8"
    )
    _run(client._maybe_apply_control())
    assert cat.resolve("100") is not None

    # 管理页新增 pB → products_version 变化 → 热重载
    _write_json(
        products,
        {"products": {
            "pA": {"name": "A", "message": "m", "source_item_id": "100"},
            "pB": {"name": "B", "message": "m", "source_item_id": "200"},
        }},
    )
    (tmp_path / "control.json").write_text(
        json.dumps({"cookie": "unb=111; cna=old", "products_version": 2}), encoding="utf-8"
    )
    _run(client._maybe_apply_control())
    assert cat.resolve("200")["key"] == "pB"
    assert client.cookies_str == "unb=111; cna=old"  # cookie 未被动
    cat.close()
