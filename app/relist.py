"""补货协调器：让同款商品保持 min_online 份在线，卖一份补一份。

成交自动补货走 RepublishManager.ensure_min_online（每单触发一次，按需
补足到 min_online）。真实发布只发生在：模板存在 + 在售不足 + 无失败。
publish/list 均可注入 fake，离线测试不触网。

CLI（真实发布前请先 --report 只读核对，再 --backfill）：
    python -m app.relist --report          # 只读：各商品在售份数/已售总数/补发历史
    python -m app.relist --backfill        # 真实：一次补满到 min_online

闲鱼必须直连：入口即清空代理环境变量（v2ray 等仅限 GitHub）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from .alerter import anotify


def clear_proxy_env() -> None:
    """清空全部代理环境变量（直连闲鱼）。可重复调用。"""
    os.environ["no_proxy"] = "*"
    os.environ["NO_PROXY"] = "*"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


class RepublishManager:
    def __init__(
        self,
        *,
        cookies_str: str = "",
        catalog=None,
        min_online: Optional[int] = None,
        publish_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        list_fn=None,
        myid: str = "",
        delay: float = 3.0,
    ) -> None:
        from .catalog import Catalog

        self.catalog = catalog if catalog is not None else Catalog()
        self.min_online = min_online if min_online is not None else self.catalog.min_online
        self.cookies_str = cookies_str or self.catalog_cookies_default()
        self.myid = myid or ""
        self._publish_fn = publish_fn
        self._list_fn = list_fn
        self.delay = delay
        self._locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def catalog_cookies_default() -> str:
        from .config import config

        return config.cookies_str

    def _lock_for(self, product_key: str) -> asyncio.Lock:
        if product_key not in self._locks:
            self._locks[product_key] = asyncio.Lock()
        return self._locks[product_key]

    # ---- 在售统计（可注入 fake）----

    async def _fetch_on_sale(self) -> List[Dict[str, Any]]:
        if self._list_fn is not None:
            result = self._list_fn(self.cookies_str, self.myid)
            if asyncio.iscoroutine(result):
                result = await result
            return list(result or [])
        from .item_read_api import fetch_on_sale_items

        return await asyncio.to_thread(fetch_on_sale_items, self.cookies_str, self.myid)

    def _is_product_item(self, on_sale: Dict[str, Any], product) -> bool:
        item_id = on_sale.get("id") or ""
        title = on_sale.get("title") or ""
        known = set(self.catalog.alias_ids(product["key"]))
        return item_id in known or (product["name"] and product["name"] in title)

    async def count_on_sale(
        self, product_key: str, *, exclude_ids: Optional[List[str]] = None
    ) -> int:
        product = self.catalog.product(product_key)
        if product is None:
            return 0
        items = await self._fetch_on_sale()
        excluded = {str(x) for x in exclude_ids or [] if x}
        return sum(
            1
            for item in items
            if str(item.get("id") or "") not in excluded
            and self._is_product_item(item, product)
        )

    # ---- 发布一份（可注入 fake）----

    async def _do_publish(self, template: Dict[str, Any]) -> Dict[str, Any]:
        if self._publish_fn is not None:
            result = self._publish_fn(template)
            if asyncio.iscoroutine(result):
                result = await result
            return dict(result or {})
        from .publish_api import Publisher

        return await asyncio.to_thread(
            Publisher(self.cookies_str).publish_copy, template
        )

    # ---- 核心：补足到 min_online ----

    async def ensure_min_online(
        self,
        product_key: str,
        *,
        ref_order: str = "",
        trigger: str = "after_sale",
        exclude_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """把商品补到 min_online 份。返回 {published, on_sale}。同一商品串行执行。

        exclude_ids：刚成交的 item_id 若仍残留在「在售」列表（平台下架有延迟），
        计数时排除它，避免把刚卖掉的单算成在线导致漏补。
        """
        async with self._lock_for(product_key):
            product = self.catalog.product(product_key)
            if product is None:
                return {"published": 0, "on_sale": 0, "error": "unknown_product"}
            if product.get("relist", True) is False:
                # 只自动发货、不自动铺货的商品：成交后不补份（不做任何统计/发布）
                return {"published": 0, "on_sale": None, "skipped": "relist_disabled"}
            template = product.get("listing")
            try:
                on_sale = await self.count_on_sale(product_key, exclude_ids=exclude_ids)
            except Exception as exc:  # noqa: BLE001  （在售列表拉取失败则本次不补发，防重复铺货）
                _logger().error(f"统计在售份数失败: product={product_key} error={type(exc).__name__}: {exc}")
                await anotify(
                    f"relist_failed:{product_key}",
                    f"补货失败：{_product_label(self.catalog, product_key)} 统计在售份数失败",
                    f"{type(exc).__name__}: {exc}",
                )
                return {"published": 0, "on_sale": None, "error": "list_failed"}
            if template is None:
                self.catalog.log_publish(
                    product_key, trigger=trigger, ref_order=ref_order,
                    ok=False, note="无发布模板（listing 未配置）",
                )
                await anotify(
                    f"relist_failed:{product_key}",
                    f"补货失败：{_product_label(self.catalog, product_key)} 无发布模板",
                    "请到管理页为该商品补 listing 发布模板（话术已设但无法自动铺货）。",
                )
                return {"published": 0, "on_sale": on_sale, "error": "no_template"}

            missing = max(0, self.min_online - on_sale)
            published = 0
            for index in range(missing):
                try:
                    result = await self._do_publish(template)
                except Exception as exc:  # noqa: BLE001
                    logger = _logger()
                    logger.error(f"补发失败: product={product_key} error={type(exc).__name__}: {exc}")
                    self.catalog.log_publish(
                        product_key, trigger=trigger, ref_order=ref_order,
                        ok=False, note=f"{type(exc).__name__}: {exc}",
                    )
                    await anotify(
                        f"relist_failed:{product_key}",
                        f"补货失败：{_product_label(self.catalog, product_key)} 发布未成功",
                        f"{type(exc).__name__}: {exc}",
                    )
                    break
                item_id = str(result.get("item_id") or "")
                if not item_id:
                    self.catalog.log_publish(
                        product_key, trigger=trigger, ref_order=ref_order,
                        ok=False, note="发布成功但未取到 item_id",
                    )
                    await anotify(
                        f"relist_failed:{product_key}",
                        f"补货失败：{_product_label(self.catalog, product_key)} 发布成功但未取到 item_id",
                        "链接已创建但无法登记别名，需人工在店铺核对后补登记。",
                    )
                    break
                self.catalog.register_alias(item_id, product_key)
                self.catalog.log_publish(
                    product_key, item_id=item_id, trigger=trigger,
                    ref_order=ref_order, ok=True, note="",
                )
                _logger().success(f"补发成功: product={product_key} 新 item_id={item_id} ({index + 1}/{missing})")
                published += 1
                if index + 1 < missing and self.delay > 0:
                    await asyncio.sleep(self.delay)

            return {"published": published, "on_sale": on_sale, "min_online": self.min_online}

    # ---- 报表（只读）----

    def report(self) -> Dict[str, Any]:
        from .config import PROJECT_ROOT

        summary = []
        for key in self.catalog.product_keys():
            product = self.catalog.product(key)
            sales = self.catalog.counts_by_product(PROJECT_ROOT / "data" / "deliveries.db").get(key, 0)
            logs = self.catalog.publish_logs(product_key=key, limit=5)
            summary.append(
                {
                    "product_key": key,
                    "name": product["name"],
                    "min_online": self.min_online,
                    "total_sales": sales,
                    "latest_publishes": [
                        {"item_id": log["item_id"], "trigger": log["trigger"],
                         "ok": log["ok"], "note": log["note"], "at": log["created_at"]}
                        for log in logs
                    ],
                }
            )
        return {"products": summary}


def _logger():
    from loguru import logger

    return logger


def _product_label(catalog, product_key: str) -> str:
    product = catalog.product(product_key) if catalog else None
    return str(product.get("name") or product_key) if product else product_key


async def relist_after_delivery(catalog, manager, result: Dict[str, Any]) -> Dict[str, Any]:
    """成交并交付成功后，把商品在线份数补足。供 ListenerClient 调用。"""
    if manager is None or catalog is None:
        return {"published": 0, "skipped": "not_enabled"}
    item_id = result.get("item_id")
    product = catalog.resolve(item_id) if item_id else None
    product_key = product.get("key") if product else None
    if not product_key:
        return {"published": 0, "skipped": "no_product"}
    return await manager.ensure_min_online(
        product_key,
        ref_order=str(result.get("order_id") or ""),
        trigger="after_sale",
        exclude_ids=[str(item_id)] if item_id else None,
    )


# ---- CLI ----

def _parse_args(argv: list) -> dict:
    out: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--report":
            out["report"] = True
        elif arg == "--backfill":
            out["backfill"] = True
        elif arg in ("--min", "--delay"):
            out[arg[2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        i += 1
    return out


def _setup_logging() -> None:
    from loguru import logger

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
    )


async def _run(argv: list) -> int:
    args = _parse_args(argv)
    if not args.get("report") and not args.get("backfill"):
        _logger().error("用法: python -m app.relist --report | --backfill")
        return 2

    from .catalog import Catalog
    from .config import config

    config.require_auth()
    clear_proxy_env()

    catalog = Catalog()
    min_arg = args.get("min")
    if min_arg:
        try:
            catalog.min_online = max(1, int(min_arg))
        except (TypeError, ValueError):
            _logger().error(f"--min 需为整数: {min_arg!r}")
            return 2
    manager = RepublishManager(cookies_str=config.cookies_str, catalog=catalog, myid=config.myid)

    if args.get("report"):
        payload = manager.report()
        # 叠加实时在售份数（只读，失败不影响其它列）
        for row in payload["products"]:
            try:
                row["on_sale_now"] = await manager.count_on_sale(row["product_key"])
            except Exception as exc:  # noqa: BLE001
                row["on_sale_now"] = f"查询失败:{type(exc).__name__}"
        _logger().info(f"商品报表(只读): {payload['products']}")
        return 0

    if args.get("backfill"):
        _logger().warning("--backfill 将真实发布商品（写操作）")
        total = 0
        for key in catalog.product_keys():
            result = await manager.ensure_min_online(key, trigger="backfill")
            total += result.get("published", 0)
            _logger().info(f"backfill {key}: {result}")
        _logger().success(f"backfill 完成，共新发布 {total} 份")
        return 0
    return 0


def main() -> None:
    _setup_logging()
    raise SystemExit(asyncio.run(_run(sys.argv[1:])))


if __name__ == "__main__":
    main()
