"""消息发送接口。

Phase 2 完全离线：禁止真实发送。这里只定义发送接口与 Mock 实现。
MockMessageSender 仅把发送请求记录到内存，不产生任何网络/业务副作用。
"""
from __future__ import annotations

from typing import Dict, List


class MessageSender:
    """发送接口。send 返回 True 表示发送成功，False 表示失败。"""

    def send(
        self,
        *,
        buyer_id: str,
        conversation_id: str,
        item_id: str,
        message: str,
    ) -> bool:
        raise NotImplementedError


class MockMessageSender(MessageSender):
    """Mock 发送器：只记录调用，不真正发送。

    fail_times 指定前 N 次 send 返回 False（模拟发送失败，用于测试）。
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.sent: List[Dict[str, str]] = []

    def send(
        self,
        *,
        buyer_id: str,
        conversation_id: str,
        item_id: str,
        message: str,
    ) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.sent.append(
            {
                "buyer_id": buyer_id,
                "conversation_id": conversation_id,
                "item_id": item_id,
                "message": message,
            }
        )
        return True

    @property
    def count(self) -> int:
        return len(self.sent)
