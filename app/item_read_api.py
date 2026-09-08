"""只读的商品列表 / 详情查询（mtop GET 语义，绝不写数据）。

- fetch_on_sale_items：卖家「我的发布-在售」列表（mtop.idle.web.xyh.item.list，
  镜像 A2 get_item_list_info / get_all_items），用于统计同款在售份数。
- fetch_item_detail：单商品详情（mtop.taobao.idle.pc.detail，唯一入参 itemId），
  供 capture 抓现有商品当发布模板。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .mtop import Mtop, MtopError

_LIST_API = "mtop.idle.web.xyh.item.list"
_LIST_VERSION = "1.0"
_DETAIL_API = "mtop.taobao.idle.pc.detail"
_DETAIL_VERSION = "1.0"


def fetch_on_sale_items(
    cookies_str: str,
    myid: Optional[str] = None,
    max_pages: int = 10,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """拉取卖家当前「在售」分组的所有商品。返回 [{id, title, pic_url, ...}]。"""
    mtop = Mtop(cookies_str, timeout=timeout)
    items: List[Dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        data_payload = {
            "needGroupInfo": False,
            "pageNumber": page,
            "pageSize": 20,
            "groupName": "在售",
            "groupId": "58877261",
            "defaultGroup": True,
            "userId": myid or "",
        }
        try:
            body = mtop.post(api=_LIST_API, version=_LIST_VERSION, data=data_payload)
        except MtopError as exc:
            if page == 1:
                raise
            break

        data = body.get("data") or {}
        card_list = data.get("cardList") or []
        page_items: List[Dict[str, Any]] = []
        for card in card_list:
            card_data = card.get("cardData") or {}
            pic_info = card_data.get("picInfo") or {}
            page_items.append(
                {
                    "id": str(card_data.get("id") or ""),
                    "title": str(card_data.get("title") or ""),
                    "pic_url": str(pic_info.get("picUrl") or "") if isinstance(pic_info, dict) else "",
                    "price": (card_data.get("priceInfo") or {}).get("price")
                    if isinstance(card_data.get("priceInfo"), dict)
                    else None,
                    "detail_url": str(card_data.get("detailUrl") or ""),
                    "card_type": card_data.get("cardType"),
                }
            )
        items.extend(page_items)
        if not card_list or len(card_list) < 20:
            break
        page += 1
    return items


def fetch_item_detail(cookies_str: str, item_id: str, timeout: float = 30.0) -> Dict[str, Any]:
    """抓取单个商品详情，返回完整响应体（字段以实际抓包为准，不做强解析）。"""
    mtop = Mtop(cookies_str, timeout=timeout)
    data_payload = {"itemId": str(item_id)}
    body = mtop.post(api=_DETAIL_API, version=_DETAIL_VERSION, data=data_payload)
    if not isinstance(body, dict):
        raise MtopError("详情返回格式异常")
    return body
