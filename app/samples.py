"""脱敏事件样本保存器。

将真实事件去除 Cookie / Token / 手机号 / 登录昵称等敏感内容后，
分类保存到 tests/fixtures/live_events/{category}/ 供研究分析。

分类：
- normal_message   普通聊天消息
- pre_payment      redReminder=等待买家付款
- waiting_ship     redReminder=等待卖家发货（已付款待发货）
- cancelled        redReminder=交易关闭
- system           其他系统消息 / 红点提醒
- other            其他
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import config

# 值级敏感字段：直接替换为占位符
_SENSITIVE_VALUE_KEYS = {
    "token",
    "accessToken",
    "authorization",
    "_m_h5_tk",
    "cookie2",
    "cna",
    "XSRF-TOKEN",
    "tracknick",
    "cookie",
    "cookies",
}

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(value: Any) -> Any:
    """递归脱敏：替换敏感 key 的值与字符串中的手机号/邮箱。"""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k in _SENSITIVE_VALUE_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        out = _PHONE_RE.sub("138****0000", value)
        out = _EMAIL_RE.sub("[email]@[redacted]", out)
        return out
    return value


def classify(parsed: Dict[str, Any]) -> str:
    reminder = parsed.get("redReminder")
    if reminder == "等待卖家发货":
        return "waiting_ship"
    if reminder == "等待买家付款":
        return "pre_payment"
    if reminder == "交易关闭":
        return "cancelled"
    if reminder:
        return "system"
    if parsed.get("event_type") == "chat":
        return "normal_message"
    return "other"


class SampleSaver:
    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir or config.fixture_dir

    def save(self, parsed: Dict[str, Any], decoded: Dict[str, Any]) -> Path | None:
        """保存脱敏样本，返回文件路径；分类为 other 时不落盘。"""
        category = classify(parsed)
        if category == "other":
            return None

        dir_path = self.fixture_dir / category
        dir_path.mkdir(parents=True, exist_ok=True)

        ts_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(":", "").replace("+00:00", "Z")
        file_path = dir_path / f"{ts_utc}.json"

        sample: Dict[str, Any] = {
            "category": category,
            "timestamp": ts_utc,
            "redReminder": parsed.get("redReminder"),
            "candidate_order_ids": parsed.get("candidate_order_ids", []),
            "candidate_buyer_ids": parsed.get("candidate_buyer_ids", []),
            "candidate_item_ids": parsed.get("candidate_item_ids", []),
            "decoded": redact(decoded),
        }
        file_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return file_path
