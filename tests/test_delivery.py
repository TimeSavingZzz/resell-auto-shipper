"""Phase 2 自动发货核心逻辑测试（完全离线，无任何网络访问）。"""
from __future__ import annotations

import pytest

from app.delivery import DeliveryService, build_delivery_key
from app.sender import MockMessageSender
from app.store import DeliveryStore

PRODUCTS = {
    "ITEM_001": {
        "name": "测试商品",
        "message": "您好，您的资料如下：\nhttps://example.com/test\n提取码：1234",
    },
    "ITEM_002": {
        "name": "测试商品二",
        "message": "您好，您购买的资料如下：\nhttps://example.com/002\n提取码：5678",
    },
}


def make_event(
    item_id=None,
    buyer_id=None,
    order_id=None,
    chat_id=None,
    reminder="等待卖家发货",
):
    return {
        "event_type": "red_reminder" if reminder else "chat",
        "redReminder": reminder,
        "chat_id": chat_id,
        "sender_id": None,
        "content": "",
        "candidate_order_ids": [order_id] if order_id else [],
        "candidate_buyer_ids": [buyer_id] if buyer_id else [],
        "candidate_item_ids": [item_id] if item_id else [],
    }


@pytest.fixture
def service():
    sender = MockMessageSender()
    store = DeliveryStore(":memory:")
    svc = DeliveryService(products=dict(PRODUCTS), sender=sender, store=store, own_id="8888")
    return svc, sender


def test_normal_payment_finds_product_and_sends_mock(service):
    svc, sender = service
    event = make_event(item_id="ITEM_001", buyer_id="2000111222", order_id="12345678901234567890")
    result = svc.handle(event)
    assert result.status == "delivered"
    assert sender.count == 1
    assert sender.sent[0]["buyer_id"] == "2000111222"
    assert sender.sent[0]["item_id"] == "ITEM_001"
    assert "https://example.com/test" in sender.sent[0]["message"]


def test_non_payment_event_not_sent(service):
    svc, sender = service
    event = make_event(item_id="ITEM_001", buyer_id="2000111222", reminder="等待买家付款")
    assert svc.handle(event).status == "not_delivery"
    event2 = make_event(item_id="ITEM_001", buyer_id="2000111222", reminder=None)
    assert svc.handle(event2).status == "not_delivery"
    assert sender.count == 0


def test_product_not_found_not_sent(service):
    svc, sender = service
    event = make_event(item_id="UNKNOWN_ITEM", buyer_id="2000111222")
    assert svc.handle(event).status == "no_product"
    assert sender.count == 0


def test_missing_buyer_not_sent(service):
    svc, sender = service
    event = make_event(item_id="ITEM_001", buyer_id=None)
    assert svc.handle(event).status == "no_buyer"
    assert sender.count == 0


def test_missing_item_not_sent(service):
    svc, sender = service
    event = make_event(item_id=None, buyer_id="2000111222")
    assert svc.handle(event).status == "no_item"
    assert sender.count == 0


def test_duplicate_event_sends_only_once(service):
    svc, sender = service
    event = make_event(item_id="ITEM_001", buyer_id="2000111222")
    statuses = [svc.handle(event).status for _ in range(10)]
    assert statuses[0] == "delivered"
    assert set(statuses[1:]) == {"already_delivered"}
    assert sender.count == 1


def test_send_failure_not_recorded_as_success(service):
    store = DeliveryStore(":memory:")
    sender = MockMessageSender(fail_times=1)
    svc = DeliveryService(products=dict(PRODUCTS), sender=sender, store=store, own_id="8888")
    event = make_event(item_id="ITEM_001", buyer_id="2000111222")

    first = svc.handle(event)
    assert first.status == "send_failed"
    assert store.already_delivered(first.delivery_key) is False

    second = svc.handle(event)
    assert second.status == "delivered"
    assert sender.count == 1  # 仅第二次成功

    third = svc.handle(event)
    assert third.status == "already_delivered"


def test_sent_event_not_sent_again(service):
    svc, sender = service
    event = make_event(item_id="ITEM_001", buyer_id="2000111222")
    assert svc.handle(event).status == "delivered"
    assert svc.handle(event).status == "already_delivered"
    assert sender.count == 1


def test_different_products_different_message(service):
    svc, sender = service
    svc.handle(make_event(item_id="ITEM_001", buyer_id="2000111222"))
    svc.handle(make_event(item_id="ITEM_002", buyer_id="2000333444"))
    assert sender.count == 2
    assert "example.com/test" in sender.sent[0]["message"]
    assert "example.com/002" in sender.sent[1]["message"]
    assert sender.sent[0]["message"] != sender.sent[1]["message"]


def test_different_buyers_sent_to_correct_recipient(service):
    svc, sender = service
    svc.handle(make_event(item_id="ITEM_001", buyer_id="2000111222", chat_id="2000111222"))
    svc.handle(make_event(item_id="ITEM_002", buyer_id="2000333444", chat_id="2000333444"))
    assert sender.count == 2
    assert [r["buyer_id"] for r in sender.sent] == ["2000111222", "2000333444"]
    assert [r["conversation_id"] for r in sender.sent] == ["2000111222", "2000333444"]


# ---- delivery_key 设计测试 ----

def test_delivery_key_prefers_order_id():
    event = make_event(item_id="ITEM_001", buyer_id="2000111222", order_id="12345678901234567890")
    assert build_delivery_key(event) == "order:12345678901234567890"


def test_delivery_key_fallback_item_buyer():
    event = make_event(item_id="ITEM_001", buyer_id="2000111222")
    assert build_delivery_key(event) == "item:ITEM_001:buyer:2000111222"


def test_delivery_key_none_without_identifiers():
    event = make_event(item_id=None, buyer_id=None)
    assert build_delivery_key(event) is None
    event2 = make_event(item_id="ITEM_001", buyer_id=None)
    assert build_delivery_key(event2) is None
