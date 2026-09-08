"""只读 WebSocket 客户端。

职责：
- 建立连接（Cookie + token 认证）
- /reg 注册、ackDiff 状态同步、/! 心跳、逐帧 ACK
- 解码同步包 → 解析事件 → 记录本地 + 保存脱敏样本
- 断线 backoff 重连；认证失败 / token 失效不无限重连

绝不发送聊天消息 / 执行任何写操作。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

import websockets
from loguru import logger

from .config import config
from .crypto import generate_device_id, generate_mid
from .decoder import decode_sync_packages, is_sync_package
from .parser import parse_event
from .protocol import (
    build_ack_frame,
    build_heartbeat_frame,
    build_reg_frame,
    build_sync_ack_frame,
    websocket_headers,
)
from .alerter import anotify
from .recorder import EventRecorder
from .samples import SampleSaver
from .token_api import TokenFetchError, fetch_access_token


class AuthError(Exception):
    """认证失败（Cookie 失效 / token 无效 / 风控），不应无限重连。"""


class ListenerClient:
    def __init__(self, cookies_str: str, token: str = "") -> None:
        self.cookies_str = cookies_str
        self.token = token
        self.myid = config.myid
        self.device_id = generate_device_id(self.myid or "unknown")
        self.base_url = config.base_url
        self.heartbeat_interval = config.heartbeat_interval
        self.heartbeat_timeout = config.heartbeat_timeout
        self.max_backoff = config.max_backoff

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._last_heartbeat_sent = 0.0
        self._last_heartbeat_response = 0.0
        self._heartbeat_task: Optional[asyncio.Task] = None

        self.recorder = EventRecorder()
        self.sample_saver = SampleSaver()

        self.auto_ship = os.getenv("AUTO_SHIP") == "1"
        self.auto_relist = os.getenv("AUTO_RELIST") == "1"
        self.trigger: Any = None
        self.catalog: Any = None
        self.relist: Any = None
        if self.auto_ship:
            # 统一用 Catalog 的 item_map（可热载）：新增/停用商品 reload 后即时生效
            from .catalog import Catalog
            from .relist import RepublishManager, clear_proxy_env
            from .trigger import AutoShipTrigger

            clear_proxy_env()
            self.catalog = Catalog()
            self.trigger = AutoShipTrigger(
                cookies_str=cookies_str, own_id=self.myid,
                products=self.catalog.item_map(),
            )
            if self.auto_relist:
                self.relist = RepublishManager(
                    cookies_str=cookies_str, catalog=self.catalog, myid=self.myid
                )
                logger.warning(
                    "AUTO_SHIP=1 AUTO_RELIST=1：待发货自动发货；成交后将自动补足同款在线份数"
                )
            else:
                logger.warning("AUTO_SHIP=1：检测到待发货事件将自动发话术并标记发货")

        # 运行期管理握手：control.json 由管理页写入，bot 低频轮询
        from .config import PROJECT_ROOT

        self._control_path = PROJECT_ROOT / "data" / "control.json"
        self._watch_task: Optional[asyncio.Task] = None
        self._cookie_refreshed = asyncio.Event()
        self._control_last_cookie = None
        self._control_last_products = None
        self._control_poll = float(os.getenv("CONTROL_POLL", "5"))
        self._relogin_poll = float(os.getenv("RELOGIN_POLL", "60"))

    # ---- 连接与事件循环 ----

    async def run(self) -> None:
        """主循环：连接 → 监听。

        XY_SINGLE_RUN=1 时为单次测试模式：任何异常立即停止，不重连。
        默认模式：断线 backoff 重连；认证失败（AuthError）不再永久退出——
        发告警后进入恢复等待：管理页写入新 Cookie 立即重连，否则每
        RELOGIN_POLL（默认 60s）自动探测一次（告警已节流，避免刷屏）。
        保留显式退出路径（单次模式 / 手动停进程）。
        """
        single = os.getenv("XY_SINGLE_RUN") == "1"
        backoff = 1.0
        watch_task = None
        if not single:
            watch_task = asyncio.create_task(self._watch_control_loop())
        try:
            while True:
                try:
                    await self._run_once()
                    if single:
                        return
                    backoff = 1.0
                except AuthError as exc:
                    logger.error(f"认证失败（不发消息、不无限重试）: {exc}")
                    await anotify(
                        "cookie_expired",
                        "闲鱼 Cookie 失效或触发风控",
                        f"{exc} 请在管理页更新 Cookie，bot 会自动恢复。",
                    )
                    if single:
                        return
                    logger.warning(
                        "等待管理页写入新 Cookie（data/control.json）…写入即自动重连"
                    )
                    self._cookie_refreshed.clear()
                    retry_wait = self._relogin_poll
                    while not self._cookie_refreshed.is_set():
                        try:
                            await asyncio.wait_for(
                                self._cookie_refreshed.wait(), timeout=retry_wait
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"Cookie 尚未更新，{int(retry_wait)}s 后自动重试一次"
                            )
                            break
                    backoff = 1.0
                except Exception as exc:
                    if single:
                        logger.error(f"单次模式: 连接终止，不重试。{type(exc).__name__}: {exc}")
                        return
                    logger.error(f"连接中断: {type(exc).__name__}: {exc} | {backoff:.0f}s 后重连")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
        finally:
            if watch_task is not None:
                watch_task.cancel()
                try:
                    await watch_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _run_once(self) -> None:
        token = self.token or self._acquire_token()
        logger.info("WebSocket connecting...")

        headers = websocket_headers(self.cookies_str)
        try:
            ws = await self._connect(headers)
        except (websockets.exceptions.InvalidStatus,) as exc:
            status = getattr(exc, "response", None)
            code = getattr(status, "status_code", None)
            if code in (401, 403):
                raise AuthError(f"WebSocket 握手被拒（HTTP {code}），Cookie/Token 可能失效") from exc
            raise
        except websockets.exceptions.ConnectionClosed:
            raise  # 非鉴权失败（网络/被顶号），交由上层退避重连，不等待人工 Cookie

        self.ws = ws
        self._last_heartbeat_sent = time.time()
        self._last_heartbeat_response = time.time()
        logger.info("WebSocket connected...")

        try:
            await self._send(build_reg_frame(token, self.device_id, self.myid))
            logger.info("已发送 /reg 注册帧")
            await asyncio.sleep(1)
            await self._send(build_sync_ack_frame())
            logger.info("已发送 /r/SyncStatus/ackDiff 状态同步")

            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._message_loop()
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await ws.close()
            except Exception:
                pass
            self.ws = None

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

    async def _message_loop(self) -> None:
        async for message in self.ws:  # type: ignore[union-attr]
            try:
                frame = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("收到非 JSON 帧，已跳过")
                continue

            if await self._handle_heartbeat_response(frame):
                continue

            ack = build_ack_frame(frame)
            if ack:
                await self._send(ack)

            if not is_sync_package(frame):
                continue

            for entry in decode_sync_packages(frame):
                if "error" in entry:
                    logger.warning(f"同步包条目 {entry['index']} 解码失败: {entry['error']}")
                    continue
                self._process_event(frame, entry["decoded"])

    # ---- 事件处理 ----

    def _process_event(self, frame: Dict[str, Any], decoded: Dict[str, Any]) -> None:
        parsed = parse_event(decoded, own_id=self.myid)
        self.recorder.record(frame=frame, decoded=decoded, parsed=parsed)
        self.sample_saver.save(parsed, decoded)

        reminder = parsed.get("redReminder")
        if reminder == "等待卖家发货":
            logger.warning("已付款待发货事件（redReminder=等待卖家发货）")

        if self.trigger is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._auto_ship_task(parsed))

        # 控制台摘要：不含 Cookie / Token，content 截断
        content = (parsed.get("content") or "")[:40]
        logger.info(
            "[EVENT] type={} redReminder={} chat_id={} "
            "candidate_order_ids={} candidate_buyer_ids={} candidate_item_ids={} content={!r}",
            parsed.get("event_type"),
            reminder,
            parsed.get("chat_id"),
            parsed.get("candidate_order_ids", []),
            parsed.get("candidate_buyer_ids", []),
            parsed.get("candidate_item_ids", []),
            content,
        )

    # ---- 自动发货 ----

    async def _auto_ship_task(self, parsed: Dict[str, Any]) -> None:
        try:
            result = await self.trigger.handle(parsed)  # type: ignore[union-attr]
            status = result.get("status")
            if status in ("delivered", "send_failed", "ship_failed"):
                logger.info(f"[自动发货] {status}: {result}")
            if status == "delivered":
                name = self._product_name(result.get("item_id"))
                await anotify(
                    "sale_success",
                    f"成交已自动发货：{name or '未知商品'}",
                    f"order={result.get('order_id', '')} buyer={result.get('buyer_id', '')}",
                )
                if self.relist is not None:
                    from .relist import relist_after_delivery

                    await relist_after_delivery(self.catalog, self.relist, result)
            elif status == "send_failed":
                await anotify(
                    "send_failed",
                    "自动发货失败：话术未发送成功",
                    f"delivery_key={result.get('delivery_key', '')}",
                )
            elif status == "ship_failed":
                await anotify(
                    "ship_failed",
                    "自动发货失败：话术已发但标记发货未成功",
                    f"delivery_key={result.get('delivery_key', '')}（需人工跟进）",
                )
        except Exception as exc:
            logger.error(f"自动发货/补货处理异常: {type(exc).__name__}: {exc}")

    def _product_name(self, item_id: Any) -> str:
        if item_id is None or self.catalog is None:
            return ""
        product = self.catalog.resolve(item_id)
        return str(product.get("name") or "") if product else ""

    # ---- 心跳 ----

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                now = time.time()
                if now - self._last_heartbeat_sent >= self.heartbeat_interval:
                    await self._send(build_heartbeat_frame())
                    self._last_heartbeat_sent = time.time()

                if now - self._last_heartbeat_response > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，判定连接失效，主动断开触发退避重连")
                    if self.ws is not None:
                        try:
                            await self.ws.close()
                        except Exception:
                            pass
                    return
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"心跳循环异常: {exc}")

    async def _handle_heartbeat_response(self, frame: Dict[str, Any]) -> bool:
        if (
            isinstance(frame, dict)
            and isinstance(frame.get("headers"), dict)
            and "mid" in frame["headers"]
            and frame.get("code") == 200
        ):
            self._last_heartbeat_response = time.time()
            return True
        return False

    # ---- 运行期热更新（管理页经 data/control.json 握手）----

    async def _watch_control_loop(self) -> None:
        """低频轮询 control.json：Cookie / products 变化 → 热刷新。"""
        while True:
            try:
                await asyncio.sleep(self._control_poll)
                await self._maybe_apply_control()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"control 轮询异常: {type(exc).__name__}: {exc}")

    async def _maybe_apply_control(self) -> None:
        try:
            data = json.loads(self._control_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"control.json 读取失败（写入可能未完成，跳过本轮）: {exc}")
            return
        if not isinstance(data, dict):
            return

        products_ver = data.get("products_version")
        if products_ver is not None and products_ver != self._control_last_products:
            self._control_last_products = products_ver
            if self.catalog is not None:
                try:
                    self.catalog.reload()
                    logger.info("管理页已更新商品，catalog 已热重载")
                except Exception as exc:
                    logger.error(f"商品热重载失败: {type(exc).__name__}: {exc}")

        cookie = str(data.get("cookie") or "").strip()
        if not cookie:
            return
        if cookie == self._control_last_cookie or cookie == self.cookies_str:
            self._control_last_cookie = cookie
            return
        self._control_last_cookie = cookie
        await self._apply_cookie(cookie)

    async def _apply_cookie(self, new_cookie: str) -> None:
        """把管理页更新的 Cookie 同步到各运行对象，断开 ws 触发按新凭证重连。"""
        logger.warning("管理页 Cookie 已更新，正在热刷新并重连")
        config.set_cookie(new_cookie)
        self.cookies_str = new_cookie
        self.myid = config.myid
        self.device_id = generate_device_id(self.myid or "unknown")
        if self.trigger is not None:
            self.trigger.cookies_str = new_cookie
        if self.relist is not None:
            self.relist.cookies_str = new_cookie
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self._cookie_refreshed.set()

    # ---- 工具 ----

    async def _send(self, data: Any) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps(data, ensure_ascii=False))

    def _acquire_token(self) -> str:
        try:
            token = fetch_access_token(self.cookies_str, self.myid, device_id=self.device_id)
            logger.info("已获取 WebSocket accessToken")
            return token
        except TokenFetchError as exc:
            raise AuthError(str(exc)) from exc
