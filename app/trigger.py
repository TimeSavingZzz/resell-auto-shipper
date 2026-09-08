"""自动发货决策器（Phase 3）：收到付款事件 → 自动发话术 → 自动标记发货。

流程：
- 每个事件先交给 OrderContextTracker 学习该会话的 order/item 上下文。
- redReminder == "等待卖家发货" 时：补全 item/order → 查商品话术 →
  防重复（SQLite order 维度）→ 真实发消息 → 成功后再真实标记发货 → 记录。

幂等：deliveries 表以 order 为 key 持久化，重连/重复事件不重复发货。
注入 send_fn/ship_fn 以便离线测试；默认走真实 WebSocket 发送 + consign.dummy。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from .config import config
from .delivery import WAIT_SHIP_REMINDER, build_delivery_key, first_buyer_id, load_products
from .order_context import OrderContextTracker
from .store import DeliveryStore

logger = logging.getLogger("autoship")


class AutoShipTrigger:
    def __init__(
        self,
        *,
        cookies_str: str = "",
        products: Optional[Dict[str, Dict[str, Any]]] = None,
        store: Optional[DeliveryStore] = None,
        own_id: str = "",
        send_fn: Optional[Callable[..., Awaitable[bool]]] = None,
        ship_fn: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        self.cookies_str = cookies_str or config.cookies_str
        self.products = products if products is not None else load_products()
        self.store = store if store is not None else DeliveryStore()
        self.own_id = own_id or config.myid
        self.tracker = OrderContextTracker()

        self._send_fn = send_fn
        self._ship_fn = ship_fn

    async def handle(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        self.tracker.observe(parsed, self.products)

        if parsed.get("redReminder") != WAIT_SHIP_REMINDER:
            return {"status": "not_waiting_ship"}

        ctx = self.tracker.resolve(parsed, self.products)
        if not ctx:
            return {"status": "unresolved", "chat_id": parsed.get("chat_id")}

        item_id = ctx["item_id"]
        buyer_id = ctx["buyer_id"]
        order_id = ctx["order_id"]
        chat_id = ctx["chat_id"]

        product = self.products.get(item_id)
        if not product:
            return {"status": "no_product", "item_id": item_id}

        if not order_id:
            return {"status": "no_order", "item_id": item_id, "buyer_id": buyer_id}

        if not buyer_id:
            buyer_id = first_buyer_id(parsed, self.own_id)
        if not buyer_id:
            return {"status": "no_buyer", "item_id": item_id}

        delivery_key = build_delivery_key(
            {
                "candidate_order_ids": [order_id],
                "candidate_item_ids": [item_id],
                "candidate_buyer_ids": [buyer_id] if buyer_id else [],
            },
            own_id=self.own_id,
        )
        if self.store.already_delivered(delivery_key):
            return {"status": "already_delivered", "delivery_key": delivery_key}

        message = product["message"]
        logger.info(
            "自动发货: buyer=%s item=%s order=%s 开始发送话术", buyer_id, item_id, order_id
        )

        sent = await self._do_send(buyer_id, chat_id, item_id, message)
        if not sent:
            return {"status": "send_failed", "delivery_key": delivery_key}

        logger.info("话术已发送，开始标记订单 %s 已发货", order_id)
        try:
            ship_ok = await self._do_ship(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("标记发货失败: order=%s error=%s", order_id, exc)
            return {"status": "ship_failed", "delivery_key": delivery_key}

        if not ship_ok:
            return {"status": "ship_failed", "delivery_key": delivery_key}

        self.store.record_delivery(
            delivery_key=delivery_key,
            order_id=order_id,
            buyer_id=buyer_id,
            item_id=item_id,
        )
        logger.info("自动发货完成: order=%s buyer=%s item=%s", order_id, buyer_id, item_id)
        return {
            "status": "delivered",
            "delivery_key": delivery_key,
            "order_id": order_id,
            "buyer_id": buyer_id,
            "item_id": item_id,
        }

    async def _do_send(self, buyer_id: str, chat_id: str, item_id: str, message: str) -> bool:
        if self._send_fn is not None:
            return bool(await self._send_fn(buyer_id=buyer_id, conversation_id=chat_id,
                                            item_id=item_id, message=message))
        from .ws_sender import RealMessageSender, SendAuthError, SendError

        sender = RealMessageSender(self.cookies_str, own_id=self.own_id)
        try:
            return await sender.send_to_buyer(buyer_id, message)
        except (SendAuthError, SendError) as exc:
            logger.error("发送失败: %s", exc)
            return False

    async def _do_ship(self, order_id: str) -> bool:
        if self._ship_fn is not None:
            return bool(await asyncio.to_thread(self._ship_fn, self.cookies_str, order_id))
        from .ship_api import ShipConfirmError, confirm_dummy_ship

        def _run() -> bool:
            try:
                confirm_dummy_ship(self.cookies_str, order_id)
                return True
            except ShipConfirmError as exc:
                logger.error("发货确认失败: order=%s error=%s", order_id, exc)
                return False

        return bool(await asyncio.to_thread(_run))
