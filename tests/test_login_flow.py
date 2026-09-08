"""管理页会话登录 / 改密码 / 登出 离线测试。

不触网、不写真实 .env：admin_token 显式注入；改密码持久化注入 fake 回调。
"""
from __future__ import annotations

import json

from app.control import create_app

TOKEN = "sekpass123"  # 8+ 位，满足新密码长度校验


def _seed_products(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "products": {
            "p1": {"key": "p1", "name": "示例", "message": "话术", "enabled": True}
        }
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make(tmp_path, token=TOKEN, persist=None):
    products = tmp_path / "config" / "products.json"
    _seed_products(products)
    app = create_app(
        products_path=products,
        relist_db=tmp_path / "data" / "relist.db",
        control_path=tmp_path / "data" / "control.json",
        deliveries_db=tmp_path / "data" / "deliveries.db",
        admin_token=token,
        fakes={"list_fn": lambda c, m: []},
        admin_token_persist=persist,
    )
    ctx = app.extensions["xy"]
    ctx.cookie_get = lambda: ""
    ctx.cookie_set = lambda c: None
    ctx.myid = lambda: ""
    return app, ctx


def test_wrong_password_401_and_no_cookie(tmp_path):
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    bad = client.post("/api/login", json={"password": "nope"})
    assert bad.status_code == 401
    # 未种会话 cookie：后续访问仍 401
    assert client.get("/api/products").status_code == 401


def test_login_sets_session_cookie_then_authorizes(tmp_path):
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    resp = client.post("/api/login", json={"password": TOKEN})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert resp.headers.get("Set-Cookie", "").startswith("xy_admin_session=")

    # 不带 header，仅凭 cookie 即可访问受保护接口
    assert client.get("/api/products").status_code == 200
    payload = client.get("/api/products").get_json()
    assert [p["key"] for p in payload["products"]] == ["p1"]


def test_header_auth_still_works_without_login(tmp_path):
    # 兼容旧方式：脚本/测试直接用 X-Admin-Token，无需先登录
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    assert client.get("/api/products", headers={"X-Admin-Token": TOKEN}).status_code == 200
    assert client.get("/api/products", headers={"X-Admin-Token": "bad"}).status_code == 401


def test_logout_clears_session(tmp_path):
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    client.post("/api/login", json={"password": TOKEN})
    assert client.get("/api/products").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/products").status_code == 401


def test_login_requires_configured_token(tmp_path):
    app, _ctx = _make(tmp_path, token="")
    client = app.test_client()
    resp = client.post("/api/login", json={"password": "anything"})
    assert resp.status_code == 403
    assert client.get("/api/products", headers={"X-Admin-Token": "anything"}).status_code == 401


def test_change_password_updates_and_persists(tmp_path):
    persisted = []
    app, ctx = _make(tmp_path, persist=persisted.append)
    client = app.test_client()
    client.post("/api/login", json={"password": TOKEN})

    new_pw = "new-secret-99"
    resp = client.post(
        "/api/password",
        json={"old_password": TOKEN, "new_password": new_pw},
        headers={"X-Admin-Token": TOKEN},
    )
    assert resp.status_code == 200, resp.get_json()
    assert ctx.admin_token == new_pw
    assert persisted == [new_pw]

    # 旧密码/旧 header 已失效
    assert client.get("/api/products", headers={"X-Admin-Token": TOKEN}).status_code == 401
    # 用新密码重登成功
    fresh = app.test_client()
    assert fresh.post("/api/login", json={"password": TOKEN}).status_code == 401
    assert fresh.post("/api/login", json={"password": new_pw}).status_code == 200
    assert fresh.get("/api/products").status_code == 200


def test_change_password_rejects_wrong_old_and_short(tmp_path):
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    client.post("/api/login", json={"password": TOKEN})

    wrong_old = client.post(
        "/api/password",
        json={"old_password": "wrong", "new_password": "abcdefgh"},
        headers={"X-Admin-Token": TOKEN},
    )
    assert wrong_old.status_code == 401

    short = client.post(
        "/api/password",
        json={"old_password": TOKEN, "new_password": "short"},
        headers={"X-Admin-Token": TOKEN},
    )
    assert short.status_code == 400

    same = client.post(
        "/api/password",
        json={"old_password": TOKEN, "new_password": TOKEN},
        headers={"X-Admin-Token": TOKEN},
    )
    assert same.status_code == 400


def test_change_password_requires_auth(tmp_path):
    app, _ctx = _make(tmp_path)
    client = app.test_client()
    resp = client.post("/api/password", json={"old_password": "x", "new_password": "yyyyyyyy"})
    assert resp.status_code == 401
