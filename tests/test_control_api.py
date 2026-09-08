"""A3 Web 管理服务（control.py）离线测试。

全部注入 tmp 路径 + fake IO，不触网、不写真实 .env / data / config。
"""
from __future__ import annotations

import json

import pytest

from app.control import create_app

TOKEN = "sek"


def _seed_products(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "products": {
            "p1": {
                "key": "p1",
                "name": "示例资料",
                "message": "话术",
                "enabled": True,
                "source_item_id": "100",
                "listing": {
                    "title": "示例资料",
                    "description": "自动发货",
                    "images": ["https://img.alicdn.com/x.jpg"],
                    "price": "9.9",
                    "delivery": "无需邮寄",
                },
            }
        }
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make(tmp_path, fakes=None, seed=True):
    products = tmp_path / "config" / "products.json"
    if seed:
        _seed_products(products)
    relist_db = tmp_path / "data" / "relist.db"
    deliveries = tmp_path / "data" / "deliveries.db"
    control = tmp_path / "data" / "control.json"
    merged = dict(fakes or {})
    merged.setdefault("list_fn", lambda c, m: [])  # 默认离线：在售列表为空
    app = create_app(
        products_path=products,
        relist_db=relist_db,
        control_path=control,
        deliveries_db=deliveries,
        admin_token=TOKEN,
        fakes=merged,
    )
    ctx = app.extensions["xy"]
    state: dict = {}
    ctx.cookie_get = lambda: state.get("cookie", "")
    ctx.cookie_set = lambda c: state.__setitem__("cookie", c)
    ctx.myid = lambda: ""
    return app, ctx, control


def _headers():
    return {"X-Admin-Token": TOKEN}


def test_auth_gate_and_products_list(tmp_path):
    app, _ctx, _control = _make(tmp_path)
    client = app.test_client()
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/products", headers={"X-Admin-Token": "bad"}).status_code == 401
    assert client.get("/api/products", headers=_headers()).status_code == 200

    page = client.get("/")
    assert page.status_code == 200
    assert "管理页" in page.get_data(as_text=True)

    payload = client.get("/api/products", headers=_headers()).get_json()
    assert [p["key"] for p in payload["products"]] == ["p1"]
    assert payload["products"][0]["enabled"] is True


def test_cookie_update_writes_control_and_denies_empty(tmp_path):
    app, _ctx, control = _make(tmp_path)
    client = app.test_client()

    bad = client.post("/api/cookie", json={"cookie": ""}, headers=_headers())
    assert bad.status_code == 400

    resp = client.post(
        "/api/cookie", json={"cookie": "unb=1; cookie2=abc"}, headers=_headers()
    )
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    data = json.loads(control.read_text(encoding="utf-8"))
    assert data["cookie"] == "unb=1; cookie2=abc"
    assert data["products_version"] == 0


def test_product_crud_bumps_version_and_reflects(tmp_path):
    app, ctx, control = _make(tmp_path)
    client = app.test_client()

    created = client.post(
        "/api/products",
        json={"key": "p2", "name": "资料B", "message": "话术B", "source_item_id": "200"},
        headers=_headers(),
    )
    assert created.status_code == 200
    assert ctx.products_version == 1

    client.put(
        "/api/products/p2", json={"enabled": False}, headers=_headers()
    )
    assert ctx.products_version == 2

    # 同 key 重复创建被拒
    dup = client.post("/api/products", json={"key": "p2", "name": "x"}, headers=_headers())
    assert dup.status_code == 409

    rows = client.get("/api/products", headers=_headers()).get_json()["products"]
    by_key = {r["key"]: r for r in rows}
    assert by_key["p2"]["enabled"] is False

    client.delete("/api/products/p1", headers=_headers())
    assert ctx.products_version == 3

    # 落盘结构 + catalog 热重载一致
    after = client.get("/api/products", headers=_headers()).get_json()["products"]
    assert [p["key"] for p in after] == ["p2"]
    assert ctx.catalog().resolve("100") is None  # p1 已删除
    assert ctx.catalog().resolve("200") is None  # p2 停用
    assert json.loads(control.read_text(encoding="utf-8"))["products_version"] == 3


def test_backfill_uses_fake_publish(tmp_path):
    published = []
    fakes = {
        "list_fn": lambda c, m: [{"id": "999", "title": "其它商品"}],
        "publish_fn": lambda template: (published.append(template) or {"item_id": "7770000000000"}),
    }
    app, _ctx, _control = _make(tmp_path, fakes=fakes)
    client = app.test_client()
    resp = client.post("/api/backfill", headers=_headers())
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["results"]["p1"]["published"] == 2  # min_online=2
    assert len(published) == 2


def test_autoship_manual_with_fakes(tmp_path):
    send_log, ship_log = [], []
    fakes = {
        "send_fn": lambda buyer, message: (send_log.append((buyer, message)) or True),
        "ship_fn": lambda cookie, order: (ship_log.append((cookie, order)) or True),
    }
    app, _ctx, _control = _make(tmp_path, fakes=fakes)
    client = app.test_client()

    # 未配 cookie：拒绝
    empty = client.post(
        "/api/autoship",
        json={"item_id": "100", "buyer_id": "b", "order_id": "o"},
        headers=_headers(),
    )
    assert empty.status_code == 400

    client.post("/api/cookie", json={"cookie": "unb=1"}, headers=_headers())

    unknown = client.post(
        "/api/autoship",
        json={"item_id": "888", "buyer_id": "b", "order_id": "o"},
        headers=_headers(),
    )
    assert unknown.status_code == 404

    missing = client.post(
        "/api/autoship", json={"item_id": "100", "buyer_id": "b"}, headers=_headers()
    )
    assert missing.status_code == 400

    ok = client.post(
        "/api/autoship",
        json={"item_id": "100", "buyer_id": "buyer1", "order_id": "order9"},
        headers=_headers(),
    )
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    assert send_log == [("buyer1", "话术")]
    assert ship_log == [("unb=1", "order9")]


def test_report_endpoint_reads_through(tmp_path):
    app, _ctx, _control = _make(tmp_path)
    client = app.test_client()
    resp = client.get("/api/report", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert "products" in body
    assert body["products"][0]["product_key"] == "p1"


def test_page_html_preserves_js_escapes():
    """_PAGE_HTML 必须是 raw 字符串：JS 内 \\' 反斜杠不能被 Python 吞掉，
    否则下发 script 引号错乱 → 整页空白只剩 title。"""
    from app.control import _PAGE_HTML
    marker = 'onclick="toggle('
    idx = _PAGE_HTML.find(marker)
    assert idx != -1 and _PAGE_HTML[idx + len(marker)] == "\\"  # '(' 后紧跟反斜杠 = \\' 保留
    assert "onclick=\"toggle(''" not in _PAGE_HTML              # 不得退化成吞反斜杠的坏写法


# ---- POST /api/cookie/test ----

def _set_cookie(client, cookie="unb=1"):
    return client.post("/api/cookie", json={"cookie": cookie}, headers=_headers())


def test_cookie_test_requires_auth(tmp_path):
    app, _ctx, _control = _make(tmp_path)
    client = app.test_client()
    assert client.post("/api/cookie/test").status_code == 401


def test_cookie_test_without_cookie_400(tmp_path):
    app, _ctx, _control = _make(tmp_path)
    client = app.test_client()
    resp = client.post("/api/cookie/test", headers=_headers())
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "no_cookie"


def test_cookie_test_ok_uses_current_cookie(tmp_path):
    app, _ctx, _control = _make(tmp_path, fakes={"cookie_check": lambda c, m: "tok"})
    client = app.test_client()
    _set_cookie(client, "unb=live")
    resp = client.post("/api/cookie/test", headers=_headers())
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert resp.get_json()["status"] == "ok"


def test_cookie_test_invalid_and_risk(tmp_path):
    from app.token_api import TokenFetchError

    def flaky(msg):
        def _check(c, m):
            raise TokenFetchError(msg)
        return _check

    app, _ctx, _control = _make(tmp_path, fakes={"cookie_check": flaky("token 接口返回失败")})
    client = app.test_client()
    _set_cookie(client)
    r1 = client.post("/api/cookie/test", headers=_headers()).get_json()
    assert r1["ok"] is False and r1["status"] == "invalid"

    app2, _ctx2, _c2 = _make(tmp_path, fakes={"cookie_check": flaky("触发风控/限流（RGV587_ERROR）")})
    client2 = app2.test_client()
    _set_cookie(client2)
    r2 = client2.post("/api/cookie/test", headers=_headers()).get_json()
    assert r2["ok"] is False and r2["status"] == "risk"


def test_cookie_test_network_indeterminate(tmp_path):
    import requests

    def _net(c, m):
        raise requests.RequestException("timeout")

    app, _ctx, _control = _make(tmp_path, fakes={"cookie_check": _net})
    client = app.test_client()
    _set_cookie(client)
    resp = client.post("/api/cookie/test", headers=_headers()).get_json()
    assert resp["ok"] is False and resp["status"] == "network"
