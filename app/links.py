"""把闲鱼/淘宝分享文案、短链接或纯数字解析成 item_id（纯函数，可离线测试）。

分享格式众多：
- 纯数字：10~15 位 item id；
- goofish 商品页：.../item?id=123... 或 .../item/123....html；
- 通用淘宝：item.htm?id=...、itemId=...；
- app「复制链接」多为短链（m.tb.cn / t.cn / uland.taobao.com），纯文本
  解析不到 id，需 resolver 跟随重定向取最终 URL 再解析（网络，由调用方注入）。

宁可解析不出也不误报：文本里若只有一串 10~15 位数字，可能是 id，也可能是
unb 等账号号段——调用方拿到结果后应再做「该 id 是否已在商品表」校验兜底。
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# 10~15 位纯数字是闲鱼 item_id 的常见区间（订单号一般 16+，避免误取）。
_DIGITS = re.compile(r"(?<!\d)(\d{10,15})(?!\d)")
# 只认 id= 系参数：itemId / item_id / 独立的 id=（前面不能是字母，避免把
# userId/ownerId/valid 之类的尾缀 "Id" 误当成 id 参数）。
_QUERY = re.compile(
    r"itemId=(\d{10,15})|item_id=(\d{10,15})|(?<![A-Za-z])id=(\d{10,15})",
    re.IGNORECASE,
)
_PATH = re.compile(r"/item/(\d{10,15})(?:\.html)?(?:[/?#]|$)", re.IGNORECASE)
# 同 _QUERY + /item/ 数字，用于整页 HTML 扫描（tb.cn 落地页把真实 goofish
# 地址内嵌在 HTML、本身不 302）。
_ANCHORED = re.compile(
    r"itemId=(\d{10,15})|item_id=(\d{10,15})|(?<![A-Za-z])id=(\d{10,15})"
    r"|/item/(\d{10,15})(?:\.html)?(?:[/?#]|$)",
    re.IGNORECASE,
)
# 分享短链域名：需要跟随重定向/解析落地页才能拿到真实 URL。
_SHORT_LINK_MARKERS = ("tb.cn", "uland.taobao.com", "taobao.com", "goofish.com")


def _first_capture(match: "re.Match") -> Optional[str]:
    if match is None:
        return None
    return next((match.group(i) for i in range(1, match.re.groups + 1) if match.group(i)), None)


def extract_item_id_from_page(text: str) -> Optional[str]:
    """从落地页/整页 HTML 里只按锚定模式提取 item_id（不碰裸数字串）。"""
    if not text:
        return None
    return _first_capture(_ANCHORED.search(text))


def extract_item_id_from_page(text: str) -> Optional[str]:
    """从落地页/整页 HTML 里只按锚定模式提取 item_id（不碰裸数字串）。"""
    if not text:
        return None
    match = _ANCHORED.search(text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def extract_item_id(text: str) -> Optional[str]:
    """从一段文本/URL 里提取 item_id；取不到返回 None。"""
    if not text:
        return None
    t = text.strip()
    if t.isdigit() and 10 <= len(t) <= 15:
        return t
    for pattern in (_QUERY, _PATH):
        match = pattern.search(t)
        if match:
            return _first_capture(match)
    # 最后兜底：独立的 10~15 位数字串。已排除带其它数字上下文的串，
    # 降低把 timestamp / 账号号段误当 item 的风险。
    match = _DIGITS.search(t)
    return match.group(1) if match else None


def looks_like_short_link(text: str) -> bool:
    """文本里含短链特征（非 item 直达 URL），可能需要跟随重定向。"""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _SHORT_LINK_MARKERS) and extract_item_id(text) is None


def resolve_item_id(text: str, resolver: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """解析 item_id；纯文本失败且疑似短链时，用 resolver(url)->落地页/URL 再试。

    resolver 可返回最终 URL（发生了重定向时），或整页 HTML（tb.cn 落地页把真实
    goofish 地址内嵌在页面里、本身不 302）；两者都会再扫一遍锚定 id 模式。
    """
    found = extract_item_id(text)
    if found:
        return found
    if resolver is None or not looks_like_short_link(text):
        return None
    try:
        landed = resolver(text.strip())
    except Exception:
        return None
    if not landed:
        return None
    found = extract_item_id(landed)
    if found:
        return found
    # 落地页整页内容：仅按 id=/itemId=/item/ 锚定匹配，避免裸数字误报
    if len(landed) > 200 or "<" in landed:
        return extract_item_id_from_page(landed)
    return None
