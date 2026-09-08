"""闲鱼商品真实发布（以现有商品抓来的 listing 模板补发一份同款）。

流程镜像参考项目 A2 utils/item_publisher.py（已验证）：
1. mtop.taobao.idle.kgraph.property.recommend 预测类目
2. mtop.taobao.idle.local.poi.get 取账号默认发货地
3. mtop.idle.pc.idleitem.publish 发布（quantity 恒 1，返回全新 item_id）

图片直接复用模板里的 CDN URL（prepare_image_for_publish 的 url 直传分支，
不上传本地文件）。纯函数 build_publish_payload / build_public_channel_payload
与网络分离，便于离线单测。

类目/地址由服务端自动给出；风控 / 令牌非法等失败抛 PublishError，不无限重试。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .mtop import Mtop, MtopError

ALLOWED_DELIVERY_CHOICES = {"包邮", "按距离计费", "一口价", "无需邮寄"}
DEFAULT_DELIVERY = "无需邮寄"


class PublishError(Exception):
    """发布失败（模板不完整 / 风控 / 业务错误等）。"""


# ---- 纯函数（离线可测）----

def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _to_cent(yuan: Any) -> str:
    """元 -> 分（字符串，参照 A2 round(current_price*100)）。"""
    return str(int(round((_as_float(yuan) or 0) * 100)))


def _parse_dim(value: Any, default: int = 800) -> int:
    try:
        number = int(float(value))
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def normalize_images(images: Any) -> List[Dict[str, Any]]:
    """把模板图片规整为发布用 imageInfo：优先 URL 直传，不做本地上传。

    入参可为 [str 链接] 或 [{"url":...} / {"src":...}]。
    """
    normalized: List[Dict[str, Any]] = []
    if not isinstance(images, list):
        return normalized
    for image in images:
        if isinstance(image, str):
            url, width, height = image, 800, 800
        elif isinstance(image, dict):
            url = str(
                image.get("url") or image.get("src") or image.get("image_url") or ""
            ).strip()
            width = _parse_dim(image.get("width") or image.get("widthSize"))
            height = _parse_dim(image.get("height") or image.get("heightSize"))
        else:
            continue
        if not url:
            continue
        if url not in [item["url"] for item in normalized]:
            normalized.append({"url": url, "width": width, "height": height})
    return normalized


def build_public_channel_payload(title: str, images_info: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "title": title,
        "lockCpv": False,
        "multiSKU": False,
        "publishScene": "mainPublish",
        "scene": "newPublishChoice",
        "description": title,
        "imageInfos": [
            {
                "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                "isQrCode": False,
                "url": image["url"],
                "heightSize": image["height"],
                "widthSize": image["width"],
                "major": True,
                "type": 0,
                "status": "done",
            }
            for image in images_info
        ],
        "uniqueCode": str(int(time.time() * 1000000)),
    }


def pick_category(card_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从推荐 cardList 里挑类目（真实接口把类目候选放 cardList[0].valuesList）。

    优先选服务端标记 isClicked=1 的首个候选，其次取首个带 catId 的候选。
    返回 {catId, catName, channelCatId, tbCatId}。
    """
    if not isinstance(card_list, list):
        return {}
    for card in card_list:
        card_data = card.get("cardData") if isinstance(card, dict) else None
        values = (card_data or {}).get("valuesList") or []
        if not isinstance(values, list):
            continue
        clicked = None
        first_with_cat = None
        for value in values:
            if not isinstance(value, dict) or not value.get("catId"):
                continue
            if first_with_cat is None:
                first_with_cat = value
            if str(value.get("isClicked")) in ("1", "true", "True"):
                clicked = value
                break
        chosen = clicked or first_with_cat
        if chosen:
            return {
                "catId": chosen.get("catId", ""),
                "catName": chosen.get("catName", ""),
                "channelCatId": chosen.get("channelCatId") or chosen.get("channelCat1Id") or "",
                "tbCatId": chosen.get("tbCatId") or "",
            }
    return {}


def build_item_labels(card_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """镜像 A2 _build_item_label_list：每个卡片取首个被点选的属性，构造宝贝标签。"""
    labels: List[Dict[str, Any]] = []
    for card in card_list or []:
        card_data = card.get("cardData") if isinstance(card, dict) else {}
        values_list = card_data.get("valuesList") or []
        if not isinstance(values_list, list):
            continue
        for value in values_list:
            if not isinstance(value, dict) or str(value.get("isClicked")) not in ("1", "true", "True"):
                continue
            labels.append(
                {
                    "channelCateName": value.get("catName"),
                    "valueId": None,
                    "channelCateId": value.get("channelCatId"),
                    "valueName": None,
                    "tbCatId": value.get("tbCatId"),
                    "subPropertyId": None,
                    "labelType": "common",
                    "subValueId": None,
                    "labelId": None,
                    "propertyName": card_data.get("propertyName"),
                    "isUserClick": "1",
                    "isUserCancel": None,
                    "from": "newPublishChoice",
                    "propertyId": card_data.get("propertyId"),
                    "labelFrom": "newPublish",
                    "text": value.get("catName"),
                    "properties": (
                        f"{card_data.get('propertyId')}##{card_data.get('propertyName')}:"
                        f"{value.get('channelCatId')}##{value.get('catName')}"
                    ),
                }
            )
            break
    return labels


def build_publish_payload(
    template: Dict[str, Any],
    category_result: Dict[str, Any],
    location: Dict[str, Any],
    labels: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构造发布 body（镜像 A2 _build_publish_payload 的固定项）。"""
    title = str(template.get("title") or "").strip()
    description = str(template.get("description") or title or "").strip()
    images = normalize_images(template.get("images"))
    delivery = str(template.get("delivery") or DEFAULT_DELIVERY)
    if delivery not in ALLOWED_DELIVERY_CHOICES:
        delivery = DEFAULT_DELIVERY

    if not title or not images:
        raise PublishError("发布模板缺少标题或图片，无法发布")

    payload: Dict[str, Any] = {
        "freebies": False,
        "itemTypeStr": "b",
        "quantity": "1",
        "simpleItem": "true",
        "imageInfoDOList": [
            {
                "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                "isQrCode": False,
                "url": image["url"],
                "heightSize": image["height"],
                "widthSize": image["width"],
                "major": True,
                "type": 0,
                "status": "done",
            }
            for image in images
        ],
        "itemTextDTO": {
            "desc": description,
            "title": title,
            "titleDescSeparate": description != title,
        },
        "itemLabelExtList": list(labels) if labels else [],
        "itemPriceDTO": {},
        "userRightsProtocols": [{"enable": False, "serviceCode": "SKILL_PLAY_NO_MIND"}],
        "itemPostFeeDTO": {
            "canFreeShipping": False,
            "supportFreight": False,
            "onlyTakeSelf": False,
        },
        "itemAddrDTO": {
            "area": location.get("area", ""),
            "city": location.get("city", ""),
            "divisionId": location.get("divisionId", 0),
            "gps": f"{location.get('longitude')},{location.get('latitude')}",
            "poiId": location.get("poiId", ""),
            "poiName": location.get("poi", ""),
            "prov": location.get("prov", ""),
        },
        "defaultPrice": False,
        "itemCatDTO": {
            "catId": str(category_result.get("catId", "")),
            "catName": str(category_result.get("catName", "")),
            "channelCatId": str(category_result.get("channelCatId", "")),
            "tbCatId": str(category_result.get("tbCatId", "")),
        },
        "uniqueCode": str(int(time.time() * 1000000)),
        "sourceId": "pcMainPublish",
        "bizcode": "pcMainPublish",
        "publishScene": "pcMainPublish",
    }

    post_fee = payload["itemPostFeeDTO"]
    if delivery == "包邮":
        post_fee["canFreeShipping"] = True
        post_fee["supportFreight"] = True
    elif delivery == "按距离计费":
        post_fee["supportFreight"] = True
        post_fee["templateId"] = "-100"
    elif delivery == "一口价":
        post_fee["supportFreight"] = True
        post_fee["postPriceInCent"] = _to_cent(template.get("post_price"))
        post_fee["templateId"] = "0"
    elif delivery == "无需邮寄":
        post_fee["templateId"] = "0"

    price_dto = payload["itemPriceDTO"]
    current = _as_float(template.get("price"))
    original = _as_float(template.get("original_price"))
    has_price = False
    if current is not None and current > 0:
        price_dto["priceInCent"] = str(int(round(current * 100)))
        has_price = True
    if original is not None and original > 0:
        price_dto["origPriceInCent"] = str(int(round(original * 100)))
        has_price = True
    if not has_price:
        payload["defaultPrice"] = True

    return payload


def extract_published_item_id(payload: Dict[str, Any]) -> Optional[str]:
    """在发布响应里递归找 ≥6 位纯数字的商品 id（镜像 A2）。"""

    def _search(node: Any) -> Optional[str]:
        if isinstance(node, dict):
            for key in ("itemId", "item_id", "idleItemId", "idleId"):
                value = node.get(key)
                text = str(value or "").strip()
                if text.isdigit() and len(text) >= 6:
                    return text
            for value in node.values():
                found = _search(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _search(item)
                if found:
                    return found
        return None

    return _search(payload.get("data")) if isinstance(payload, dict) else None


# ---- 真实发布 ----

class Publisher:
    def __init__(self, cookies_str: str, timeout: float = 30.0) -> None:
        self.mtop = Mtop(cookies_str, timeout=timeout)

    def publish_copy(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """发布一份同款，成功返回 {"item_id": ..., "title": ...}，失败抛 PublishError。"""
        title = str(template.get("title") or "").strip()
        description = str(template.get("description") or "").strip()
        images = normalize_images(template.get("images"))
        if not title:
            raise PublishError("发布模板缺少标题（title）")
        if not images:
            raise PublishError("发布模板缺少可用图片，无法发布（请先抓详情或补图）")

        # 1) 类目预测
        try:
            channel_body = self.mtop.post(
                api="mtop.taobao.idle.kgraph.property.recommend",
                version="2.0",
                data=build_public_channel_payload(title, images),
                spm_cnt="a21ybx.publish.0.0",
                spm_pre="a21ybx.item.sidebar.1.67321598K9Vgx8",
            )
        except MtopError as exc:
            raise PublishError(f"类目预测失败: {exc}") from exc
        channel_data = channel_body.get("data") or {}
        # 类目推荐：服务端多数走 categoryPredictResult，实测落空时候选在 cardList[0]
        category_result = (channel_data.get("categoryPredictResult") or {}).copy()
        labels = build_item_labels(channel_data.get("cardList"))
        if not category_result.get("catId"):
            category_result = pick_category(channel_data.get("cardList"))
        if not category_result.get("catId"):
            raise PublishError(f"类目预测未返回 catId: {channel_body.get('ret')}")

        # 2) 默认发货地
        try:
            location_body = self.mtop.post(
                api="mtop.taobao.idle.local.poi.get",
                version="1.0",
                data={"longitude": 118.78248347393424, "latitude": 31.91629189813543},
                spm_cnt="a21ybx.publish.0.0",
                spm_pre="a21ybx.item.sidebar.1.38262218ame5nr",
                extra_headers={"eagleeye-userdata": "spm-cnt=a21ybx"},
            )
        except MtopError as exc:
            raise PublishError(f"获取默认地址失败: {exc}") from exc
        addresses = (location_body.get("data") or {}).get("commonAddresses") or []
        if not addresses:
            raise PublishError("未获取到账号默认发货地址")
        location = addresses[0]

        # 3) 发布
        payload = build_publish_payload(template, category_result, location, labels=labels)
        try:
            publish_body = self.mtop.post(
                api="mtop.idle.pc.idleitem.publish",
                version="1.0",
                data=payload,
                spm_cnt="a21ybx.publish.0.0",
                spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
            )
        except MtopError as exc:
            raise PublishError(f"发布失败: {exc}") from exc

        item_id = extract_published_item_id(publish_body)
        if not item_id:
            raise PublishError(f"发布成功但未取到新 item_id: ret={publish_body.get('ret')}")
        return {"item_id": item_id, "title": title, "price": template.get("price")}
