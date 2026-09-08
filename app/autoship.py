"""单次真实自动发货执行器（Phase 3）。

用法：
    python -m app.autoship --item 2000000000000 --buyer 2000000000001 --order 9000000000000000001

流程：读商品话术 → 经 WebSocket 真实发送给买家（等服务器 ACK）→ ACK 成功后
调用 consign.dummy 把订单标记已发货。

强制直连闲鱼：运行即清空代理环境变量，禁止 v2ray 等走闲鱼流量。
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(key, None)

from loguru import logger  # noqa: E402


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> "
            "| <level>{message}</level>"
        ),
    )


def _parse_args(argv: list) -> dict:
    out = {}
    i = 0
    while i < len(argv):
        if argv[i] in ("--item", "--buyer", "--order"):
            out[argv[i][2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            i += 1
    return out


async def run() -> int:
    args = _parse_args(sys.argv[1:])
    item_id = args.get("item")
    buyer_id = args.get("buyer")
    order_id = args.get("order")
    missing = [k for k, v in {"item": item_id, "buyer": buyer_id, "order": order_id}.items() if not v]
    if missing:
        logger.error(f"缺少参数: {', '.join('--' + m for m in missing)}")
        return 2

    from .config import config
    from .delivery import load_products
    from .ship_api import ShipConfirmError, confirm_dummy_ship
    from .ws_sender import RealMessageSender, SendAuthError, SendError

    config.require_auth()
    cookies_str = config.cookies_str

    products = load_products()
    product = products.get(item_id)
    if not product:
        logger.error(f"products.json 中未配置商品 {item_id}")
        return 3
    message = product["message"]

    # 1) 真实发送消息
    sender = RealMessageSender(cookies_str, token="", own_id=config.myid)
    logger.info(f"[1/2] 向买家 {buyer_id} 发送商品「{product['name']}」话术")
    try:
        sent_ok = await sender.send_to_buyer(buyer_id, message)
    except (SendAuthError, SendError) as exc:
        logger.error(f"[1/2] 发送失败（不标记发货）: {exc}")
        return 1
    if not sent_ok:
        logger.error("[1/2] 发送未获服务器 ACK（结果未知），不标记发货")
        return 1

    # 2) 标记已发货
    logger.info(f"[2/2] 调用 consign.dummy 标记订单 {order_id} 已发货")
    try:
        result = confirm_dummy_ship(cookies_str, order_id)
    except ShipConfirmError as exc:
        logger.error(f"[2/2] 发货确认失败: {exc}")
        return 1
    logger.success(f"自动发货完成: order_id={result['order_id']}")
    return 0


def main() -> None:
    _setup_logging()
    code = asyncio.run(run())
    raise SystemExit(code)


if __name__ == "__main__":
    main()
