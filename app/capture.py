"""从现有闲鱼商品抓发布模板（只读），回填 config/products.json 的 listing。

用法：
    python -m app.capture --item 2000000000000              # 只读：详情落盘 logs/capture_*.json + 打印提取结果
    python -m app.capture --item 2000000000000 --price 12.9 --apply
        # 依据详情自动补 title/images 并以 --price 定价，写入 products.json listing（需人工核对后再发布）

说明：
- 只读抓取 mtop.taobao.idle.pc.detail（唯一入参 itemId），不改任何闲鱼数据；
- title 缺失时用 products.json 的 name 兜底；price 必须显式给 --price（分币不能猜）；
- 图片（CDN URL）直接从详情复用，无需本地上传；若详情取不到图则拒绝 apply。

闲鱼必须直连：入口即清空代理环境变量（v2ray 等仅限 GitHub）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(key, None)


def _logger():
    from loguru import logger

    return logger


# ---- 详情提取（启发式，字段以真实抓包为准）----

def _find_subtree(root: Any, key_name: str) -> Optional[dict]:
    """深度优先找第一个名为 key_name 的 dict。"""
    if isinstance(root, dict):
        if key_name in root and isinstance(root[key_name], dict):
            return root[key_name]
        for value in root.values():
            found = _find_subtree(value, key_name)
            if found is not None:
                return found
    elif isinstance(root, list):
        for value in root:
            found = _find_subtree(value, key_name)
            if found is not None:
                return found
    return None


_IMAGE_KEYS = {
    "picUrl", "picURL", "imageUrl", "image_url", "url", "pic", "img",
    "image", "src", "fullPicPath", "imgUrl", "mainPicUrl", "mainImg",
}
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _looks_like_image_url(text: str) -> bool:
    low = text.lower()
    return (
        text.startswith(("http://", "https://"))
        and ("alicdn" in low or "taobaocdn" in low or "goofish" in low or low.endswith(_IMAGE_EXT))
    )


def _collect_image_urls(node: Any, out: Optional[List[str]] = None) -> List[str]:
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _looks_like_image_url(value) and value not in out:
                out.append(value)
            if isinstance(value, (dict, list)):
                _collect_image_urls(value, out)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, str) and _looks_like_image_url(value) and value not in out:
                out.append(value)
            elif isinstance(value, (dict, list)):
                _collect_image_urls(value, out)
    return out


def _first_text(node: dict, keys: tuple) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_from_detail(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    item = _find_subtree(data, "itemDO") or {}
    title = _first_text(item, ("title", "itemTitle", "titleName"))
    desc = _first_text(item, ("description", "desc", "itemDescription", "content"))
    images = _collect_image_urls(item)
    # 价格候选（浅层找含 price 的标量 / 文本）
    price_candidates: List[Any] = []
    if isinstance(item, dict):
        for key, value in item.items():
            if "price" in str(key).lower() and not isinstance(value, (dict, list)):
                price_candidates.append({key: value})
    return {
        "ret": (payload.get("ret") if isinstance(payload, dict) else None),
        "title": title,
        "description": desc,
        "images": images[:9],
        "image_count": len(images),
        "price_candidates": price_candidates,
    }


# ---- config 更新 ----

def _load_products(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _locate_entry(data: dict, item_id: str) -> Optional[tuple]:
    """返回 (container, key)。兼容旧扁平与新 {products:{...}} 两种格式。"""
    containers = [data]
    if isinstance(data.get("products"), dict):
        containers = [data["products"]]
    for container in containers:
        for key, value in container.items():
            if not isinstance(value, dict):
                continue
            if str(value.get("key") or key) == item_id or value.get("source_item_id") == item_id:
                return container, key
    return None


def _to_yuan_text(price: Any) -> str:
    text = re.sub(r"[^\d.]", "", str(price or ""))
    return text


def build_listing_from_detail(
    payload: Dict[str, Any],
    *,
    item_id: str = "",
    name: str = "",
    price: str = "",
    description: str = "",
    delivery: str = "无需邮寄",
) -> Dict[str, Any]:
    """把某商品的详情响应体转成可发布的 listing 模板（供登记后自动铺货）。

    价格必须由调用方显式给（不从详情猜，避免标错价）；无可用图片时拒绝，
    因为 publisher 无法空图发布。title 缺失时回退 name/item_id。
    """
    info = extract_from_detail(payload)
    images = info["images"]
    if not images:
        raise ValueError("详情中未取到可用图片，无法生成发布模板，自动铺货不可用")
    price_text = _to_yuan_text(price)
    if not price_text:
        raise ValueError("自动铺货需先填写定价 price（元）")
    title = info["title"] or name or item_id
    desc = (description or "").strip() or info["description"] or (
        f"【自动发货资料】{name or title}，拍下后系统自动发送网盘链接。"
    )
    return {
        "title": title.strip(),
        "description": desc.strip(),
        "images": images,
        "price": price_text,
        "delivery": delivery.strip() or "无需邮寄",
    }


def write_listing(item_id: str, listing: Dict[str, Any]) -> None:
    from .config import PROJECT_ROOT

    path = PROJECT_ROOT / "config" / "products.json"
    data = _load_products(path)
    located = _locate_entry(data, item_id)
    if located is None:
        # 无既有条目：按旧扁平格式新增
        data[str(item_id)] = {"name": item_id, "message": ""}
        located = (data, str(item_id))
    container, key = located
    entry = container[key]
    if not isinstance(entry, dict):
        entry = {}
        container[key] = entry
    entry["listing"] = listing
    if not entry.get("name"):
        entry["name"] = listing.get("title") or item_id
    if "key" not in entry:
        entry["key"] = item_id
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv: list) -> dict:
    out: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--item", "--price", "--desc", "--out", "--delivery"):
            out[arg[2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif arg == "--apply":
            out["apply"] = True
        else:
            i += 1
    return out


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def run(argv: list) -> int:
    _setup_logging()
    args = _parse_args(argv)
    item_id = args.get("item")
    if not item_id:
        _logger().error("用法: python -m app.capture --item <itemId> [--price 元 --apply]")
        return 2

    from .config import PROJECT_ROOT, config

    config.require_auth()
    cookies_str = config.cookies_str

    from .item_read_api import fetch_item_detail

    _logger().info(f"只读抓取商品详情: item={item_id}")
    try:
        payload = fetch_item_detail(cookies_str, item_id)
    except Exception as exc:  # noqa: BLE001
        _logger().error(f"抓取详情失败: {type(exc).__name__}: {exc}")
        return 1

    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.get("out") or "") if args.get("out") else logs_dir / f"capture_{item_id}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    info = extract_from_detail(payload)
    _logger().info(f"详情已落盘: {out_path}")
    _logger().info(f"ret={info['ret']} title={info['title']!r} desc_len={len(info['description'])}")
    _logger().info(f"图片 {info['image_count']} 张: {info['images'][:3]}")
    if info["price_candidates"]:
        _logger().info(f"价格候选: {info['price_candidates'][:5]}")

    apply = bool(args.get("apply"))
    if not apply:
        _logger().info("未 apply（仅只读）。确认后用 --price 定价 + --apply 写入 listing。")
        return 0

    images = info["images"]
    if not images:
        _logger().error("详情中未取到可用图片，无法生成发布模板（需提供现成图 URL/文件后重试）")
        return 3

    price = args.get("price")
    if not price:
        _logger().error("--apply 需显式 --price <元> 定价（不从详情猜价，避免标错价）")
        return 3

    prod_data = _load_products(PROJECT_ROOT / "config" / "products.json")
    located = _locate_entry(prod_data, item_id)
    name = ""
    if located is not None:
        entry_value = located[0][located[1]]
        if isinstance(entry_value, dict):
            name = str(entry_value.get("name") or "")
    title = info["title"] or name or item_id
    description = (
        args.get("desc")
        or info["description"]
        or f"【自动发货资料】{name or title}，拍下后系统自动发送网盘链接。"
    )
    delivery = args.get("delivery") or "无需邮寄"
    listing = {
        "title": title,
        "description": description.strip(),
        "images": images,
        "price": _to_yuan_text(price),
        "delivery": delivery,
    }
    write_listing(item_id, listing)
    key = located[1] if located else item_id
    _logger().success(
        f"已写入 listing: key={key} title={title!r} price={listing['price']} images={len(images)}"
    )
    _logger().success("请人工核对 config/products.json 的 listing 后再执行补货（--backfill）。")
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
