"""Phase 1 只读闲鱼事件监听器入口。

运行：python -m app.main
作用：连接 WebSocket → 注册/同步/心跳/ACK → 解码并记录事件。
只读：不发送聊天消息、不自动回复、不自动发货、不修改任何数据。
"""
from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from .config import config


def _setup_logging() -> None:
    # Windows 控制台默认 GBK 会导致中文日志乱码，统一改用 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logger.remove()
    level = os.getenv("LOG_LEVEL", "INFO")
    logger.add(
        sys.stderr,
        level=str(level).upper(),
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> "
            "| <level>{message}</level>"
        ),
    )


async def _run() -> None:
    from .client import AuthError, ListenerClient

    config.require_auth()
    cookies_str = config.cookies_str
    token = config._token

    logger.info("Phase 1 只读监听器启动（不会发送任何消息）")
    logger.info(f"WebSocket 目标: {config.base_url}")
    if config.myid:
        logger.info(f"账号 unb 已读取（仅本地校验，不回显 Cookie）")

    client = ListenerClient(cookies_str, token=token)
    await client.run()


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("用户中断，退出")
    except SystemExit as exc:
        logger.error(str(exc))


if __name__ == "__main__":
    main()
