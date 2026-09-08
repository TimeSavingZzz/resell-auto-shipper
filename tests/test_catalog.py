"""Catalog：商品目录 + item_id 别名注册表 离线测试。"""
from __future__ import annotations

import json

from app.catalog import Catalog

LEGACY = {
    "2000000000000": {
        "name": "示例学习资料",
        "message": "网盘资料链接",
    }
}


def _write(path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _catalog(tmp_path, data=None, db=":memory:", min_online: int = 2):
    products = tmp_path / "products.json"
    _write(products, data if data is not None else LEGACY)
    return Catalog(products_path=products, db_path=db, min_online=min_online)


def test_legacy_flat_format_load(tmp_path):
    cat = _catalog(tmp_path)
    items = cat.item_map()
    assert "2000000000000" in items
    assert items["2000000000000"]["name"] == "示例学习资料"
    assert items["2000000000000"]["message"] == "网盘资料链接"
    record = cat.resolve("2000000000000")
    assert record["key"] == "2000000000000"
    assert cat.product_keys() == ["2000000000000"]
    assert cat.listing_template("2000000000000") is None
    cat.close()


def test_new_format_aliases_and_min_online(tmp_path):
    data = {
        "min_online": 3,
        "products": {
            "p1": {
                "name": "P1",
                "message": "话术",
                "source_item_id": "100",
                "aliases": ["200", "300"],
            }
        },
    }
    cat = _catalog(tmp_path, data=data)
    assert cat.min_online == 3
    for item_id in ("100", "200", "300"):
        record = cat.resolve(item_id)
        assert record is not None and record["key"] == "p1"
    cat.close()


def test_register_alias_immediately_visible(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.register_alias("888777666555", "2000000000000") is True
    assert cat.resolve("888777666555")["key"] == "2000000000000"
    assert "888777666555" in cat.alias_ids("2000000000000")
    assert cat.register_alias("999999999999", "unknown_product") is False
    cat.close()


def test_alias_persists_across_reopen(tmp_path):
    db = str(tmp_path / "relist.db")
    cat = _catalog(tmp_path, db=db)
    cat.register_alias("1231231231234", "2000000000000")
    cat.close()
    cat2 = _catalog(tmp_path, db=db)
    assert cat2.resolve("1231231231234") is not None
    cat2.close()


def test_unknown_item_resolves_none(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.resolve("999999999999999999999") is None
    cat.close()
