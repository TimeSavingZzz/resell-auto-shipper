"""RepublishManager 补货协调器离线测试（注入 fake 发布/在售列表）。"""
from __future__ import annotations

import asyncio
import json

from app.catalog import Catalog
from app.relist import RepublishManager

PRODUCT_KEY = "2000000000000"
PRODUCT_NAME = "示例学习资料"


def _product(with_listing: bool = True) -> dict:
    entry = {"name": PRODUCT_NAME, "message": "网盘资料链接"}
    if with_listing:
        entry["listing"] = {
            "title": PRODUCT_NAME,
            "description": "自动发货资料",
            "images": ["https://img.alicdn.com/x.jpg"],
            "price": "12.9",
            "delivery": "无需邮寄",
        }
    return {PRODUCT_KEY: entry}


def _catalog(tmp_path, data=None):
    path = tmp_path / "products.json"
    path.write_text(json.dumps(data or _product(), ensure_ascii=False), encoding="utf-8")
    return Catalog(products_path=path, db_path=str(tmp_path / "relist.db"), min_online=2)


class CounterPub:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, template):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return {"item_id": f"9{self.calls:012d}"}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _on_sale(item_ids, name=PRODUCT_NAME):
    return lambda cookies, myid: [{"id": i, "title": f"{name} 现货"} for i in item_ids]


def _make(catalog, *, pub=None, list_fn=None):
    return RepublishManager(
        cookies_str="cookies_dummy",
        catalog=catalog,
        publish_fn=pub or CounterPub(),
        list_fn=list_fn or _on_sale([]),
    )


def test_ensure_publishes_until_min_online(tmp_path):
    catalog = _catalog(tmp_path)
    pub = CounterPub()
    # 在售 1 份（配置里的历史 id），min_online=2 → 补 1
    manager = _make(catalog, pub=pub, list_fn=_on_sale([PRODUCT_KEY]))
    result = run(manager.ensure_min_online(PRODUCT_KEY, trigger="backfill"))
    assert result["published"] == 1
    assert result["on_sale"] == 1
    assert pub.calls == 1
    new_id = "9000000000001"
    assert catalog.resolve(new_id)["key"] == PRODUCT_KEY
    logs = catalog.publish_logs(product_key=PRODUCT_KEY)
    assert logs and logs[0]["ok"] is True and logs[0]["item_id"] == new_id


def test_no_publish_when_already_at_min(tmp_path):
    catalog = _catalog(tmp_path)
    pub = CounterPub()
    manager = _make(catalog, pub=pub, list_fn=_on_sale([PRODUCT_KEY, "2002002002002"]))
    result = run(manager.ensure_min_online(PRODUCT_KEY))
    assert result["published"] == 0
    assert pub.calls == 0


def test_sold_item_still_in_list_is_excluded(tmp_path):
    """成交后商品短暂残留「在售」列表：排除刚成交 id，避免漏补。"""
    catalog = _catalog(tmp_path)
    catalog.register_alias("2002002002003", PRODUCT_KEY)
    pub = CounterPub()
    # 列表含刚成交的 PRODUCT_KEY（残留）与另一份真在售 → 排除后有效在售 1
    manager = _make(catalog, pub=pub, list_fn=_on_sale([PRODUCT_KEY, "2002002002003"]))
    result = run(manager.ensure_min_online(PRODUCT_KEY, exclude_ids=[PRODUCT_KEY]))
    assert result["published"] == 1
    assert pub.calls == 1


def test_publish_failure_stops_and_logs(tmp_path):
    catalog = _catalog(tmp_path)
    pub = CounterPub(fail=True)
    manager = _make(catalog, pub=pub, list_fn=_on_sale([]))
    result = run(manager.ensure_min_online(PRODUCT_KEY))
    assert result["published"] == 0
    assert pub.calls == 1  # 只试 1 次即停，不无限重试
    logs = catalog.publish_logs(product_key=PRODUCT_KEY)
    assert logs and logs[0]["ok"] is False and "boom" in logs[0]["note"]


def test_missing_listing_template_logs_and_returns_error(tmp_path):
    catalog = _catalog(tmp_path, data=_product(with_listing=False))
    pub = CounterPub()
    manager = _make(catalog, pub=pub, list_fn=_on_sale([]))
    result = run(manager.ensure_min_online(PRODUCT_KEY))
    assert result["error"] == "no_template"
    assert pub.calls == 0


def test_unknown_product_returns_error(tmp_path):
    catalog = _catalog(tmp_path)
    manager = _make(catalog)
    result = run(manager.ensure_min_online("does-not-exist"))
    assert result["error"] == "unknown_product"


def test_list_failure_skips_publish(tmp_path):
    catalog = _catalog(tmp_path)
    pub = CounterPub()

    def broken(cookies, myid):
        raise RuntimeError("list down")

    manager = _make(catalog, pub=pub, list_fn=broken)
    result = run(manager.ensure_min_online(PRODUCT_KEY))
    assert result["error"] == "list_failed"
    assert pub.calls == 0
