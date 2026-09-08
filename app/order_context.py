"""会话 order/item 上下文学习器。

真实的"等待卖家发货"简化事件只带买家会话（decoded['1'] 为
"{buyer}@goofish"），不含 order_id / item_id。而紧邻的成交/聊天推送帧
（needPush）携带 order_id 与 item_id。本模块按 chat_id 记录最近一次见到的
order/item，供待发货事件到达时补全。

匹配只做保守启发：item 需落在已知商品表，否则不强行认定（宁缺勿发，
发货不可逆）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional


class OrderContextTracker:
    def __init__(self) -> None:
        # chat_id -> {"order_id": ..., "item_id": ..., "ts": ...}
        self._ctx: Dict[str, Dict[str, Any]] = {}

    def observe(self, parsed: Dict[str, Any], products: Dict[str, Any]) -> None:
        """从任意事件学习会话的 order/item 上下文。"""
        chat_id = parsed.get("chat_id")
        if not chat_id:
            return
        order_id = self._pick_order(parsed.get("candidate_order_ids") or [])
        item_id = self._pick_item(
            parsed.get("candidate_item_ids") or [],
            orders=parsed.get("candidate_order_ids") or [],
            chat_id=chat_id,
            products=products,
        )
        entry = self._ctx.get(chat_id)
        if entry is None:
            entry = {}
            self._ctx[chat_id] = entry
        if order_id:
            entry["order_id"] = order_id
        if item_id:
            entry["item_id"] = item_id
        if order_id or item_id:
            entry["ts"] = time.monotonic()

    def resolve(self, parsed: Dict[str, Any], products: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """为待发货事件补全 item/order。

        优先事件自带候选；缺失时回退该会话最近学习的上下文。item 必须命中
        商品表才返回，否则返回 None（宁缺勿发）。
        """
        chat_id = parsed.get("chat_id") or ""
        buyer_id = None
        for bid in parsed.get("candidate_buyer_ids") or []:
            if bid and bid != chat_id:
                buyer_id = bid
                break
        if not buyer_id and chat_id:
            buyer_id = chat_id

        order_id = self._pick_order(parsed.get("candidate_order_ids") or [])
        item_id = self._pick_item(
            parsed.get("candidate_item_ids") or [],
            orders=parsed.get("candidate_order_ids") or [],
            chat_id=chat_id,
            products=products,
        )
        entry = self._ctx.get(chat_id) or {}
        if not order_id:
            order_id = entry.get("order_id")
        if not item_id:
            item_id = entry.get("item_id")

        if not item_id or item_id not in products:
            return None
        return {
            "buyer_id": buyer_id or "",
            "chat_id": chat_id,
            "order_id": order_id or "",
            "item_id": item_id,
        }

    @staticmethod
    def _pick_order(orders: list) -> Optional[str]:
        """取第一个 16 位以上纯数字作为 order（区别于用户/商品 id）。"""
        for o in orders:
            s = str(o).strip()
            if s.isdigit() and len(s) >= 16:
                return s
        return orders[0] if orders else None

    @staticmethod
    def _pick_item(
        candidates: list,
        *,
        orders: list,
        chat_id: str,
        products: Dict[str, Any],
    ) -> Optional[str]:
        """从候选里挑商品 id：优先命中商品表；其次排除 order/会话 id。"""
        skip = set(str(o).strip() for o in orders) | {chat_id}
        for it in candidates:
            s = str(it).strip()
            if s in products:
                return s
        for it in candidates:
            s = str(it).strip()
            if not s or s in skip:
                continue
            if s.isdigit() and 10 <= len(s) <= 15:
                return s
        return None
