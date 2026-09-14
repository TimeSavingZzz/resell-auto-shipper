"""定时主动探测 Cookie 有效性（独立于 bot 的兜底检测）。

配合 systemd timer（默认每 12 小时一次）运行；也可手动 `python -m app.cookiewatch` 跑一次。

判定规则：
- 业务性失效（session 过期 / RGV587 风控 / 令牌非法等 TokenFetchError）
  → 走 alerter 告警（微信 + 邮件），退出码 1；
- 纯网络抖动（连不上 / 超时等 requests 异常）→ 仅记日志、不当失效误报，退出码 0；
- 未配置 COOKIES_STR → 仅记日志，退出码 2。

kind 复用 bot 的 "cookie_expired"，语义一致；alerter 已按 kind 节流。
绝不发送 cookie/token 明文。
"""
from __future__ import annotations

import sys
from typing import Any, Callable

import requests
from loguru import logger

from .alerter import notify
from .token_api import TokenFetchError, fetch_access_token


def probe(
    cookies_str: str,
    myid: str,
    *,
    check: Callable[..., str] = fetch_access_token,
    notify_fn: Callable[..., None] = notify,
) -> int:
    """探测一次 cookie。返回退出码：0=正常/网络抖动，1=失效/风控，2=无 cookie。"""
    if not cookies_str:
        logger.warning("未配置 COOKIES_STR，跳过 Cookie 主动检测")
        return 2
    try:
        check(cookies_str, myid)
    except TokenFetchError as exc:
        text = str(exc)
        risk = ("RGV587" in text) or ("风控" in text) or ("被挤爆" in text)
        title = "闲鱼 Cookie 触发风控（定时检测）" if risk else "闲鱼 Cookie 失效（定时检测）"
        logger.error(f"Cookie 主动检测判定失效: {text}")
        notify_fn("cookie_expired", title, text)
        return 1
    except requests.RequestException as exc:
        logger.warning(f"Cookie 检测网络不可达，非失效，下次再试: {type(exc).__name__}: {exc}")
        return 0
    logger.info("Cookie 主动检测通过：token 获取成功")
    return 0


def main(argv: Any = None) -> int:
    from .config import config

    return probe(config.cookies_str, config.myid)


if __name__ == "__main__":
    sys.exit(main())
