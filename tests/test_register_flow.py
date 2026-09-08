"""「登记已发布商品 → 自动发货」端点 + relist=false 跳过铺货的离线测试。

不触网：注入 detail_fn/url_resolver fake；count_on_sale 走默认 list_fn 空列表。
"""
from __future__ import annotations

import asyncio
import json

from app.control import _first_url, create_app
from app.catalog import Catalog
from app.relist import RepublishManager

TOKEN = "sek"
GOOD_LINK = "https://www.goofish.com/item?id=5556667777"
SRC_ID = "100000000001"  # 已登记 p1 的 source（真实闲鱼 id 为 10~15 位）


def _seed_products(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "products": {
            "p1": {
                "key": "p1", "name": "示例资料", "message": "话术",
                "enabled": True, "source_item_id": SRC_ID,
                "listing": {"title": "示例", "price": "9.9"},
            }
        }
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _detail(_cookie, item_id: str) -> dict:
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "itemDO": {
                "title": "高等数学讲义",
                "description": "电子资料 拍下自动发货",
                "picUrl": "https://img.alicdn.com/imgextra/i1/x.jpg",
                "fullPicPath": "https://img.alicdn.com/imgextra/i2/y.jpg",
                "itemId": item_id,
            }
        },
    }


def _make(tmp_path, fakes=None):
    products = tmp_path / "config" / "products.json"
    _seed_products(products)
    merged = dict(fakes or {})
    merged.setdefault("list_fn", lambda c, m: [])  # 在售为空，避免触网
    app = create_app(
        products_path=products,
        relist_db=tmp_path / "data" / "relist.db",
        control_path=tmp_path / "data" / "control.json",
        deliveries_db=tmp_path / "data" / "deliveries.db",
        admin_token=TOKEN,
        fakes=merged,
    )
    ctx = app.extensions["xy"]
    state: dict = {}
    ctx.cookie_get = lambda: state.get("cookie", "")
    ctx.cookie_set = lambda c: state.__setitem__("cookie", c)
    ctx.myid = lambda: ""
    return app, ctx, products


def _headers():
    return {"X-Admin-Token": TOKEN}


def _entries_file(products):
    return json.loads(products.read_text(encoding="utf-8"))["products"]


def test_resolve_endpoint_id_and_link(tmp_path):
    app, _ctx, _products = _make(tmp_path)
    client = app.test_client()
    by_id = client.post("/api/items/resolve", json={"text": "6941234567890"}, headers=_headers())
    assert by_id.status_code == 200 and by_id.get_json()["item_id"] == "6941234567890"
    assert by_id.get_json()["known"] is False

    by_link = client.post("/api/items/resolve", json={"text": GOOD_LINK}, headers=_headers())
    assert by_link.get_json()["item_id"] == "5556667777"

    # 已登记 item：known + 归属商品
    known = client.post("/api/items/resolve", json={"text": SRC_ID}, headers=_headers()).get_json()
    assert known["known"] is True and known["product_key"] == "p1"

    bad = client.post("/api/items/resolve", json={"text": "随便写写"}, headers=_headers())
    assert bad.status_code == 400


def test_preview_fetches_detail_with_cookie(tmp_path):
    app, _ctx, products = _make(tmp_path, fakes={"detail_fn": _detail})
    client = app.test_client()
    client.post("/api/cookie", json={"cookie": "unb=1"}, headers=_headers())

    resp = client.post("/api/items/preview", json={"text": GOOD_LINK}, headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["item_id"] == "5556667777"
    assert body["title"] == "高等数学讲义"
    assert body["image_count"] >= 2
    # preview 是只读的：不写 products
    assert _entries_file(products).keys() == {"p1"}


def test_preview_without_cookie_400(tmp_path):
    app, _ctx, _products = _make(tmp_path, fakes={"detail_fn": _detail})
    client = app.test_client()
    resp = client.post("/api/items/preview", json={"text": GOOD_LINK}, headers=_headers())
    assert resp.status_code == 400
    assert "Cookie" in resp.get_json()["error"]


def test_register_with_relist_builds_listing(tmp_path):
    app, ctx, products = _make(tmp_path, fakes={"detail_fn": _detail})
    client = app.test_client()
    client.post("/api/cookie", json={"cookie": "unb=1"}, headers=_headers())

    resp = client.post(
        "/api/items/register",
        json={
            "text": GOOD_LINK,
            "name": "高数资料",
            "message": "夸克分享链接",
            "price": "9.9",
            "auto_relist": True,
        },
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True and body["key"] == "5556667777"
    assert body["relist"] is True
    assert ctx.products_version == 1

    entry = _entries_file(products)["5556667777"]
    assert entry["name"] == "高数资料"
    assert entry["message"] == "夸克分享链接"
    assert entry["enabled"] is True
    assert entry["source_item_id"] == "5556667777"
    assert entry["relist"] is True
    assert entry["listing"]["title"] == "高等数学讲义"  # 详情素材抓成发布模板
    assert entry["listing"]["price"] == "9.9"
    assert len(entry["listing"]["images"]) >= 2

    # 热重载后能命中该 item（未来成交自动发货用）
    assert ctx.catalog().resolve("5556667777") is not None
    # GET 列表可见
    listed = [p["key"] for p in client.get("/api/products", headers=_headers()).get_json()["products"]]
    assert "5556667777" in listed


def test_register_without_relist_skips_detail_and_network(tmp_path):
    app, _ctx, products = _make(tmp_path)  # 未配 detail_fn/cookie：若触网会失败
    client = app.test_client()
    resp = client.post(
        "/api/items/register",
        json={
            "text": "8800112233",
            "name": "纯发货链接",
            "message": "hi",
            "auto_relist": False,
        },
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.get_json()
    entry = _entries_file(products)["8800112233"]
    assert entry["relist"] is False
    assert "listing" not in entry
    assert entry["message"] == "hi"


def test_first_url_pulls_short_link_from_share_text():
    text = "【某平台】https://m.tb.cn/h.8KQTXyZ?tk=abc12345 CA381 「示例商品…」 点击打开"
    assert _first_url(text) == "https://m.tb.cn/h.8KQTXyZ?tk=abc12345"


def test_first_url_plain_and_none():
    assert _first_url("https://www.goofish.com/item?id=5556667777") == "https://www.goofish.com/item?id=5556667777"
    assert _first_url("没有链接只有中文") == ""
    assert _first_url(None) == ""
    assert _first_url("") == ""


def test_register_duplicate_item_rejected(tmp_path):
    app, _ctx, _products = _make(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/items/register",
        json={"text": SRC_ID, "name": "重复", "message": "x", "auto_relist": False},
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert "p1" in resp.get_json()["error"]


def test_register_requires_message(tmp_path):
    app, _ctx, _products = _make(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/items/register",
        json={"text": "8800112233", "auto_relist": False},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "话术" in resp.get_json()["error"]


def test_register_relist_requires_images(tmp_path):
    def no_img(_cookie, item_id):
        return {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"title": "无图商品"}}}

    app, _ctx, _products = _make(tmp_path, fakes={"detail_fn": no_img})
    client = app.test_client()
    client.post("/api/cookie", json={"cookie": "unb=1"}, headers=_headers())
    resp = client.post(
        "/api/items/register",
        json={"text": GOOD_LINK, "message": "x", "price": "1", "auto_relist": True},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "图片" in resp.get_json()["error"]


def test_register_relist_requires_price(tmp_path):
    app, _ctx, _products = _make(tmp_path, fakes={"detail_fn": _detail})
    client = app.test_client()
    client.post("/api/cookie", json={"cookie": "unb=1"}, headers=_headers())
    resp = client.post(
        "/api/items/register",
        json={"text": GOOD_LINK, "message": "x", "price": "", "auto_relist": True},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "定价" in resp.get_json()["error"]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_relist_disabled_skips_ensure_min_online(tmp_path):
    products = tmp_path / "products.json"
    products.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "products": {
            "pOnly": {
                "key": "pOnly", "name": "只发货不铺货", "message": "m",
                "source_item_id": "400", "relist": False,
            },
            "pNoTmpl": {
                "key": "pNoTmpl", "name": "有铺货但无模板", "message": "m",
                "source_item_id": "500",
            },
        }
    }
    products.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    cat = Catalog(products_path=products, db_path=str(tmp_path / "relist.db"), min_online=2)
    calls = {"publish": 0, "list": 0}
    mgr = RepublishManager(
        cookies_str="unb=1",
        catalog=cat,
        publish_fn=lambda t: calls.__setitem__("publish", calls["publish"] + 1) or {},
        list_fn=lambda c, m: calls.__setitem__("list", calls["list"] + 1) or [],
    )
    result = _run(mgr.ensure_min_online("pOnly", trigger="after_sale"))
    assert result["skipped"] == "relist_disabled"
    assert calls == {"publish": 0, "list": 0}  # 不计数、不发布、不告警
    # 对照：relist 默认开启但无模板的商品仍走原 no_template 语义
    result2 = _run(mgr.ensure_min_online("pNoTmpl", trigger="after_sale"))
    assert result2.get("error") == "no_template"
    assert calls["list"] == 1 and calls["publish"] == 0
    cat.close()
