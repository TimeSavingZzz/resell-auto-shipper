"""Phase 2 最小化自动发货核心。

流程：付款事件 → redReminder=="等待卖家发货" → 提取 item_id → 商品匹配
→ 提取 buyer → 生成 delivery_key → 防重复检查 → 发送 → 记录。

完全离线：仅依赖本地 products.json + SQLite，禁止网络访问与真实发送。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT
from .sender import MessageSender, MockMessageSender
from .store import DeliveryStore

WAIT_SHIP_REMINDER = "等待卖家发货"


@dataclass
class DeliveryResult:
    status: str
    delivery_key: Optional[str] = None
    item_id: Optional[str] = None
    buyer_id: Optional[str] = None
    message: str = ""


def load_products(path=None) -> Dict[str, Dict[str, Any]]:
    if path is None:
        path = PROJECT_ROOT / "config" / "products.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def first_item_id(parsed: Dict[str, Any]) -> Optional[str]:
    ids = parsed.get("candidate_item_ids") or []
    return ids[0] if ids else None


def first_order_id(parsed: Dict[str, Any]) -> Optional[str]:
    ids = parsed.get("candidate_order_ids") or []
    return ids[0] if ids else None


def first_buyer_id(parsed: Dict[str, Any], own_id: str = "") -> Optional[str]:
    ids = parsed.get("candidate_buyer_ids") or []
    for bid in ids:
        if bid and bid != own_id:
            return bid
    return ids[0] if ids else None


def build_delivery_key(parsed: Dict[str, Any], own_id: str = "") -> Optional[str]:
    """构造防重复 key。

    优先级：order_id 候选（若存在）→ item_id + buyer_id 组合。
    order_id 候选为 Phase 1 未验证的候选值，故默认用 item+buyer 组合；
    待真实"等待卖家发货"样本确认 order_id 稳定后再优先切换。
    """
    order = first_order_id(parsed)
    if order:
        return f"order:{order}"
    item = first_item_id(parsed)
    buyer = first_buyer_id(parsed, own_id)
    if item and buyer:
        return f"item:{item}:buyer:{buyer}"
    return None


class DeliveryService:
    def __init__(
        self,
        products: Optional[Dict[str, Dict[str, Any]]] = None,
        sender: Optional[MessageSender] = None,
        store: Optional[DeliveryStore] = None,
        own_id: str = "",
    ) -> None:
        self.products = products if products is not None else load_products()
        self.sender = sender if sender is not None else MockMessageSender()
        self.store = store if store is not None else DeliveryStore()
        self.own_id = own_id

    def handle(self, parsed: Dict[str, Any]) -> DeliveryResult:
        # 1. 付款/待发货判断
        if parsed.get("redReminder") != WAIT_SHIP_REMINDER:
            return DeliveryResult("not_delivery")

        # 2. 提取 item_id
        item_id = first_item_id(parsed)
        if not item_id:
            return DeliveryResult("no_item")

        # 3. 商品匹配
        product = self.products.get(item_id)
        if not product:
            return DeliveryResult("no_product", item_id=item_id)

        # 4. 提取 buyer
        buyer_id = first_buyer_id(parsed, self.own_id)
        if not buyer_id:
            return DeliveryResult("no_buyer", item_id=item_id)

        # 5. 防重复 key
        delivery_key = build_delivery_key(parsed, self.own_id)
        if not delivery_key:
            return DeliveryResult("no_key", item_id=item_id, buyer_id=buyer_id)

        # 6. 防重复检查
        if self.store.already_delivered(delivery_key):
            return DeliveryResult(
                "already_delivered", delivery_key=delivery_key, item_id=item_id, buyer_id=buyer_id
            )

        # 7. 发送
        conversation_id = parsed.get("chat_id") or buyer_id
        message = product["message"]
        ok = self.sender.send(
            buyer_id=buyer_id,
            conversation_id=conversation_id,
            item_id=item_id,
            message=message,
        )
        if not ok:
            return DeliveryResult(
                "send_failed", delivery_key=delivery_key, item_id=item_id, buyer_id=buyer_id
            )

        # 8. 记录（失败/冲突不视为已发送）
        order_id = first_order_id(parsed)
        recorded = self.store.record_delivery(
            delivery_key=delivery_key,
            order_id=order_id,
            buyer_id=buyer_id,
            item_id=item_id,
        )
        if not recorded:
            return DeliveryResult(
                "already_delivered", delivery_key=delivery_key, item_id=item_id, buyer_id=buyer_id
            )

        return DeliveryResult(
            "delivered", delivery_key=delivery_key, item_id=item_id, buyer_id=buyer_id
        )
