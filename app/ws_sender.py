"""真实发送器：通过 WebSocket 发送文本消息。

帧为 /r/MessageSend/sendByReceiverScope，格式严格依据参考项目 A2
（XianyuAutoAsync.py:12839 send_msg）。每次发送开一条专用连接：
reg → ackDiff → 发送 → 等待服务器对发送帧的 ACK（code 200 且
headers.mid == 发送帧 mid）→ 关闭。仅收到匹配 ACK 才视为成功。

只在收到明确业务写授权后启用（Phase 3 自动发货）。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import websockets
from loguru import logger

from .config import config
from .crypto import generate_device_id, generate_mid
from .protocol import (
    build_reg_frame,
    build_send_message_frame,
    build_sync_ack_frame,
    websocket_headers,
)
from .token_api import TokenFetchError, fetch_access_token


class SendAuthError(Exception):
    """发送认证失败（cookie/token 失效），不可盲目重试。"""


class SendError(Exception):
    """发送失败（服务器拒绝 / 超时无 ACK 等）。"""


class RealMessageSender:
    def __init__(
        self,
        cookies_str: str,
        token: str = "",
        own_id: str = "",
        base_url: str = "",
    ) -> None:
        self.cookies_str = cookies_str
        self.token = token
        self.own_id = own_id or config.myid
        self.base_url = base_url or config.base_url
        self.device_id = generate_device_id(self.own_id or "unknown")

    async def send_to_buyer(self, buyer_id: str, text: str, timeout: float = 25.0) -> bool:
        """向 buyer_id 发送一条文本消息。服务器 ACK 匹配则返回 True。"""
        token = self.token or self._acquire_token()
        headers = websocket_headers(self.cookies_str)

        try:
            ws = await self._connect(headers)
        except websockets.exceptions.InvalidStatus as exc:
            status = getattr(exc, "response", None)
            code = getattr(status, "status_code", None)
            if code in (401, 403):
                raise SendAuthError(f"发送握手被拒（HTTP {code}），cookie/token 可能失效") from exc
            raise SendError(f"发送握手失败（HTTP {code}）: {exc}") from exc
        except websockets.exceptions.ConnectionClosed as exc:
            raise SendError(f"发送握手阶段连接被关闭: {exc}") from exc

        frame = build_send_message_frame(buyer_id, self.own_id, text)
        send_mid = (frame["headers"] or {}).get("mid", "")
        sent_uuid = ((frame.get("body") or [{}])[0]).get("uuid", "")

        try:
            await ws.send(json.dumps(build_reg_frame(token, self.device_id, self.own_id)))
            await asyncio.sleep(1)
            await ws.send(json.dumps(build_sync_ack_frame()))
            await asyncio.sleep(0.5)

            logger.info(f"发送消息帧: to={buyer_id} uuid={sent_uuid} len={len(text)}")
            await ws.send(json.dumps(frame, ensure_ascii=False))

            return await self._wait_ack(ws, send_mid, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except SendError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            raise SendError(f"发送过程中连接被关闭: {exc}") from exc
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def _wait_ack(self, ws, send_mid: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed as exc:
                raise SendError(f"等待 ACK 时连接被关闭: {exc}") from exc

            try:
                frame = json.loads(message)
            except json.JSONDecodeError:
                continue

            headers = frame.get("headers") if isinstance(frame, dict) else None
            mid = headers.get("mid") if isinstance(headers, dict) else None

            if mid == send_mid:
                code = frame.get("code")
                if code == 200:
                    logger.info(f"发送已获服务器 ACK: mid={send_mid}")
                    return True
                raise SendError(f"服务器拒绝发送: code={code} frame={str(frame)[:200]}")

            # 被动观察其它帧，便于调试发送路径（不回显敏感字段）
            if isinstance(frame, dict):
                ret = frame.get("ret") or frame.get("code")
                logger.debug(f"观察帧: code={frame.get('code')} ret={str(ret)[:80]}")
        raise SendError(f"发送超时（{timeout:.0f}s 内未收到匹配 ACK），结果未知")

    async def _connect(self, headers: Dict[str, str]):
        """兼容不同 websockets 版本（v14+ 用 additional_headers）。"""
        try:
            return await websockets.connect(
                self.base_url,
                additional_headers=headers,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            )
        except TypeError:
            return await websockets.connect(
                self.base_url,
                extra_headers=headers,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            )

    def _acquire_token(self) -> str:
        try:
            token = fetch_access_token(self.cookies_str, self.own_id, device_id=self.device_id)
            logger.info("已获取 WebSocket accessToken")
            return token
        except TokenFetchError as exc:
            raise SendAuthError(str(exc)) from exc
