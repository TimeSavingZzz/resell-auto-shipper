"""发布 payload 纯函数离线测试（不触网）。"""
from __future__ import annotations

from app.publish_api import (
    build_publish_payload,
    build_public_channel_payload,
    extract_published_item_id,
    normalize_images,
)

CATEGORY = {
    "catId": "50000103",
    "catName": "资料",
    "channelCatId": "60000103",
    "tbCatId": "70000103",
}
LOCATION = {
    "area": "区",
    "city": "市",
    "divisionId": 3201,
    "longitude": 118.78,
    "latitude": 31.91,
    "poiId": "poi1",
    "poi": "某地",
    "prov": "省",
}


def _template(**over):
    base = {
        "title": "示例学习资料",
        "description": "自动发货网盘资料",
        "images": ["https://img.alicdn.com/imgextra/x1.jpg"],
        "price": "12.9",
        "delivery": "无需邮寄",
    }
    base.update(over)
    return base


def test_normalize_images_string_and_dedup():
    images = normalize_images(
        ["https://img.alicdn.com/a.jpg", {"url": "https://img.alicdn.com/b.jpg", "width": 400}]
    )
    assert [img["url"] for img in images] == [
        "https://img.alicdn.com/a.jpg",
        "https://img.alicdn.com/b.jpg",
    ]
    assert images[1]["width"] == 400
    dup = normalize_images(["https://img.alicdn.com/a.jpg", "https://img.alicdn.com/a.jpg"])
    assert len(dup) == 1


def test_build_publish_payload_key_fields():
    payload = build_publish_payload(_template(), CATEGORY, LOCATION)
    assert payload["quantity"] == "1"
    assert payload["itemTypeStr"] == "b"
    assert payload["imageInfoDOList"][0]["url"] == "https://img.alicdn.com/imgextra/x1.jpg"
    assert payload["itemTextDTO"]["title"] == "示例学习资料"
    assert payload["itemTextDTO"]["desc"] == "自动发货网盘资料"
    assert payload["itemTextDTO"]["titleDescSeparate"] is True
    assert payload["itemPriceDTO"]["priceInCent"] == "1290"
    assert payload["defaultPrice"] is False
    assert payload["itemAddrDTO"]["prov"] == "省"
    assert payload["itemAddrDTO"]["gps"] == "118.78,31.91"
    assert payload["itemCatDTO"]["catId"] == "50000103"
    assert payload["itemPostFeeDTO"]["templateId"] == "0"
    assert payload["itemPostFeeDTO"]["canFreeShipping"] is False


def test_missing_price_uses_default_price():
    payload = build_publish_payload(_template(price=None), CATEGORY, LOCATION)
    assert payload["defaultPrice"] is True
    assert "priceInCent" not in payload["itemPriceDTO"]


def test_missing_images_raises():
    import pytest

    with pytest.raises(Exception):
        build_publish_payload(_template(images=[]), CATEGORY, LOCATION)


def test_extract_published_item_id_recursive():
    body = {"data": [{"something": {"idleItemId": 123456789012}}]}
    assert extract_published_item_id(body) == "123456789012"
    assert extract_published_item_id({"data": {"x": 1}}) is None


def test_build_public_channel_payload():
    images = [{"url": "https://img.alicdn.com/a.jpg", "width": 800, "height": 800}]
    channel = build_public_channel_payload("标题", images)
    assert channel["scene"] == "newPublishChoice"
    assert channel["title"] == "标题"
    assert channel["imageInfos"][0]["widthSize"] == 800
