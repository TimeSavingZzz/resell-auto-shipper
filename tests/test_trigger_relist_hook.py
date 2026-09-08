"""交付成功后触发补货（relist_after_delivery）离线测试。"""
from __future__ import annotations

import asyncio
import json

from app.catalog import Catalog
from app.relist import relist_after_delivery

PRODUCT_KEY = "2000000000000"


def _catalog(tmp_path):
    path = tmp_path / "products.json"
    data = {
        PRODUCT_KEY: {
            "name": "示例学习资料",
            "message": "网盘资料链接",
            "listing": {
                "title": "示例学习资料",
                "description": "自动发货资料",
                "images": ["https://img.alicdn.com/x.jpg"],
                "price": "12.9",
                "delivery": "无需邮寄",
            },
        }
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Catalog(products_path=path, db_path=str(tmp_path / "relist.db"), min_online=2)


class StubManager:
    def __init__(self):
        self.calls = []

    async def ensure_min_online(
        self, product_key, *, ref_order="", trigger="after_sale", exclude_ids=None
    ):
        self.calls.append((product_key, ref_order, trigger, exclude_ids))
        return {"published": 1, "on_sale": 1}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_delivered_result_triggers_relist_once(tmp_path):
    catalog = _catalog(tmp_path)
    manager = StubManager()
    result = {
        "status": "delivered",
        "item_id": PRODUCT_KEY,
        "order_id": "3316829413005003371",
    }
    out = run(relist_after_delivery(catalog, manager, result))
    assert out["published"] == 1
    assert manager.calls == [
        (PRODUCT_KEY, "3316829413005003371", "after_sale", [PRODUCT_KEY])
    ]


def test_unknown_item_skips(tmp_path):
    catalog = _catalog(tmp_path)
    manager = StubManager()
    out = run(relist_after_delivery(catalog, manager, {"item_id": "9999999999999999"}))
    assert out["skipped"] == "no_product"
    assert manager.calls == []


def test_disabled_when_manager_none(tmp_path):
    catalog = _catalog(tmp_path)
    out = run(relist_after_delivery(catalog, None, {"item_id": PRODUCT_KEY}))
    assert out["skipped"] == "not_enabled"


def test_new_alias_id_after_publish_maps_to_product(tmp_path):
    catalog = _catalog(tmp_path)
    catalog.register_alias("8800000000001", PRODUCT_KEY)
    manager = StubManager()
    out = run(
        relist_after_delivery(
            catalog, manager, {"item_id": "8800000000001", "order_id": "1"}
        )
    )
    assert out["published"] == 1
    assert manager.calls[0][0] == PRODUCT_KEY
