"""自动发货决策器离线测试（无网络，注入 mock 发送/发货）。"""
from __future__ import annotations

import asyncio

import pytest

from app.store import DeliveryStore
from app.trigger import AutoShipTrigger

PRODUCTS = {
    "2000000000000": {
        "name": "示例学习资料",
        "message": "网盘资料链接",
    }
}

# 复刻真实样本（2026-09-02 14:22 成交消息 + 待发货事件）
TRADE = {
    "event_type": "chat",
    "redReminder": None,
    "chat_id": "2000000000001",
    "candidate_order_ids": ["9000000000000000001"],
    "candidate_buyer_ids": ["2000000000002", "2000000000001"],
    "candidate_item_ids": ["2000000000000", "2000000000001", "9000000000000000001"],
}

WAITING_SHIP = {
    "event_type": "red_reminder",
    "redReminder": "等待卖家发货",
    "chat_id": "2000000000001",
    "candidate_buyer_ids": ["2000000000001"],
    "candidate_order_ids": [],
    "candidate_item_ids": [],
}


class _Fakes:
    def __init__(self):
        self.sent = []
        self.shipped = []
        self.send_fail = False
        self.ship_fail = False

    async def send(self, **kw):
        if self.send_fail:
            return False
        self.sent.append(kw)
        return True

    def ship(self, cookies_str, order_id):
        if self.ship_fail:
            return {"error": "boom"}
        self.shipped.append(order_id)
        return {"success": True, "order_id": order_id}


def _make_trigger(fakes):
    return AutoShipTrigger(
        products=dict(PRODUCTS),
        store=DeliveryStore(":memory:"),
        own_id="100000000",
        send_fn=fakes.send,
        ship_fn=fakes.ship,
    )


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_observe_then_waiting_ship_delivers_once():
    fakes = _Fakes()
    trig = _make_trigger(fakes)

    assert run(trig.handle(TRADE))["status"] == "not_waiting_ship"  # 只学习上下文
    r1 = run(trig.handle(WAITING_SHIP))
    assert r1["status"] == "delivered"
    assert r1["order_id"] == "9000000000000000001"
    assert r1["item_id"] == "2000000000000"
    assert r1["buyer_id"] == "2000000000001"

    assert len(fakes.sent) == 1
    assert fakes.sent[0]["buyer_id"] == "2000000000001"
    assert fakes.shipped == ["9000000000000000001"]

    # 重复事件/重连补发：幂等不再重复
    r2 = run(trig.handle(WAITING_SHIP))
    assert r2["status"] == "already_delivered"
    assert len(fakes.sent) == 1
    assert fakes.shipped == ["9000000000000000001"]


def test_waiting_ship_without_context_is_unresolved():
    fakes = _Fakes()
    trig = _make_trigger(fakes)
    r = run(trig.handle(WAITING_SHIP))
    assert r["status"] == "unresolved"
    assert fakes.sent == []
    assert fakes.shipped == []


def test_waiting_ship_unknown_product_skipped():
    fakes = _Fakes()
    trig = _make_trigger(fakes)
    run(trig.handle({**TRADE, "candidate_item_ids": ["9999999999999"]}))
    r = run(trig.handle(WAITING_SHIP))
    assert r["status"] == "unresolved"
    assert fakes.sent == []


def test_send_failure_prevents_ship():
    fakes = _Fakes()
    fakes.send_fail = True
    trig = _make_trigger(fakes)
    run(trig.handle(TRADE))
    r = run(trig.handle(WAITING_SHIP))
    assert r["status"] == "send_failed"
    assert fakes.shipped == []
