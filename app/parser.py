"""事件解析器。

将解密后的消息结构化为事件记录，提取：
- event_type
- redReminder
- candidate_order_ids / candidate_buyer_ids / candidate_item_ids（均为候选值，
  基于正则兜底提取；Phase 0 结论：order_id 目前没有统一可靠来源，故不认定任何单一值）
- 辅助字段（chat_id、sender、content 等）
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse

# ---- 候选 order_id 正则（依据 A2/A3 实现）----
_ORDER_PATTERNS = (
    r"(?:bizOrderId|biz_order_id|orderId|order_id)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_-]{6,})",
    r"(?:bizOrderId|biz_order_id|orderId|order_id)=([^&\\s\"']+)",
    r"(?:order-detail|order_detail|orderDetail)[\"']?\s*[:=]?\s*[\"']?[^&\\s\"']*[?&]?(?:id|orderId)=([0-9]{10,})",
)
_UPDATE_KEY_RE = re.compile(r'updateKey["\']?\s*[:=]\s*["\']([^"\']+)')
_LONG_DIGIT_RE = re.compile(r"(?<!\d)\d{16,}(?!\d)")

# ---- 候选 item_id 正则 ----
_ITEM_URL_RE = re.compile(r"(?:itemId|item_id|itemid|id)=([^&\\s\"']+)")
_ITEM_QUERY_KEYS = ("itemId", "item_id", "itemid", "id")


def _unique(items: List[Any]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        if it is None:
            continue
        s = str(it).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _dedup(ids: List[str]) -> List[str]:
    return _unique(ids)


def extract_order_ids(message: Any) -> List[str]:
    """从整条消息（递归）收集候选 order_id。结果均为候选值。"""
    candidates: List[str] = []
    raw_text = _stringify(message)

    for pattern in _ORDER_PATTERNS:
        for m in re.finditer(pattern, raw_text, re.IGNORECASE):
            candidates.append(unquote(m.group(1)))

    for m in _UPDATE_KEY_RE.finditer(raw_text):
        update_value = m.group(1)
        long_digits = _LONG_DIGIT_RE.search(update_value)
        if long_digits:
            candidates.append(long_digits.group(0))

    # 兜底：整段文本中的 16+ 位纯数字
    for m in _LONG_DIGIT_RE.finditer(raw_text):
        candidates.append(m.group(0))

    return _dedup(candidates)


def extract_item_ids(message: Any) -> List[str]:
    """收集候选 item_id：优先 reminderUrl 的查询参数，其次正则兜底。"""
    candidates: List[str] = []
    raw_text = _stringify(message)

    def from_url(url_val: Any) -> None:
        if not isinstance(url_val, str) or not url_val:
            return
        parsed = urlparse(url_val)
        query = parse_qs(parsed.query)
        for key in _ITEM_QUERY_KEYS:
            values = query.get(key)
            if values and values[0]:
                candidates.append(values[0])
        for m in _ITEM_URL_RE.finditer(url_val):
            candidates.append(unquote(m.group(1)))

    _walk(message, from_url)

    for m in _ITEM_URL_RE.finditer(raw_text):
        candidates.append(unquote(m.group(1)))

    return _dedup(candidates)


def _walk(value: Any, visitor) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "reminderUrl":
                visitor(v)
            if isinstance(v, (dict, list)):
                _walk(v, visitor)
    elif isinstance(value, list):
        for v in value:
            _walk(v, visitor)


def extract_buyer_ids(message: Any, own_id: str = "") -> List[str]:
    """收集候选 buyer_id。

    来源（均未验证为"卖家"身份，仅记录候选）：
    - message['1']['10']['senderUserId']
    - message['3']['userId']
    - message['1'] 为字符串时的 @ 前缀（会话 sid，非必然 buyer）
    - message['1']['2'] 的 @ 前缀（chat_id）
    """
    candidates: List[str] = []
    if not isinstance(message, dict):
        return candidates

    payload_1 = message.get("1")
    payload_3 = message.get("3")

    if isinstance(payload_1, dict):
        payload_10 = payload_1.get("10") if isinstance(payload_1.get("10"), dict) else {}
        if payload_10.get("senderUserId"):
            candidates.append(payload_10["senderUserId"])
        chat_raw = payload_1.get("2")
        if isinstance(chat_raw, str) and "@" in chat_raw:
            candidates.append(chat_raw.split("@", 1)[0])
    elif isinstance(payload_1, str):
        if "@" in payload_1:
            candidates.append(payload_1.split("@", 1)[0])

    if isinstance(payload_3, dict) and payload_3.get("userId"):
        candidates.append(payload_3["userId"])

    return _dedup(candidates)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _is_chat_message(message: Dict[str, Any]) -> bool:
    payload_1 = message.get("1")
    return (
        isinstance(payload_1, dict)
        and isinstance(payload_1.get("10"), dict)
        and "reminderContent" in payload_1["10"]
    )


def _is_typing_status(message: Dict[str, Any]) -> bool:
    payload_1 = message.get("1")
    return (
        isinstance(payload_1, list)
        and len(payload_1) > 0
        and isinstance(payload_1[0], dict)
        and isinstance(payload_1[0].get("1"), str)
        and "@goofish" in payload_1[0]["1"]
    )


def parse_event(decoded: Dict[str, Any], own_id: str = "") -> Dict[str, Any]:
    """将解密消息解析为事件记录结构。"""
    red_reminder = None
    if isinstance(decoded.get("3"), dict):
        red_reminder = decoded["3"].get("redReminder")
    if not isinstance(red_reminder, str) or not red_reminder.strip():
        red_reminder = None

    event_type = "unknown"
    if red_reminder:
        event_type = "red_reminder"
    elif _is_chat_message(decoded):
        event_type = "chat"
    elif _is_typing_status(decoded):
        event_type = "typing"
    elif isinstance(decoded.get("3"), dict):
        event_type = "system"

    chat_id = None
    sender_id = None
    content = ""
    item_id_from_url = None

    payload_1 = decoded.get("1")
    if isinstance(payload_1, dict):
        chat_raw = payload_1.get("2")
        if isinstance(chat_raw, str):
            chat_id = chat_raw.split("@", 1)[0]
        payload_10 = payload_1.get("10") if isinstance(payload_1.get("10"), dict) else {}
        sender_id = payload_10.get("senderUserId")
        content = payload_10.get("reminderContent") or ""
        url_info = payload_10.get("reminderUrl")
        if isinstance(url_info, str) and "itemId=" in url_info:
            item_id_from_url = url_info.split("itemId=")[1].split("&")[0]
    elif isinstance(payload_1, str):
        chat_id = payload_1.split("@", 1)[0] if "@" in payload_1 else payload_1

    candidate_order_ids = extract_order_ids(decoded)
    candidate_buyer_ids = extract_buyer_ids(decoded, own_id)
    candidate_item_ids = extract_item_ids(decoded)
    if item_id_from_url and item_id_from_url not in candidate_item_ids:
        candidate_item_ids.insert(0, item_id_from_url)

    return {
        "event_type": event_type,
        "redReminder": red_reminder,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "content": content,
        "candidate_order_ids": candidate_order_ids,
        "candidate_buyer_ids": candidate_buyer_ids,
        "candidate_item_ids": candidate_item_ids,
    }
