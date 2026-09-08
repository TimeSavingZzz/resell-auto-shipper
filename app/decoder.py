"""WebSocket 帧 → 解密消息的解码器。

流程（依据 Phase 0 研究确认）：外层帧取 body.syncPushPackage.data[]，
对每条 data 先 base64 解码；若能直接按 UTF-8 JSON 解析则视为明文，
否则走 MessagePack / 加密解密得到 JSON 字符串，再解析为 dict。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from .crypto import decrypt


class DecodeError(Exception):
    """单条同步包无法解码（不致命，跳过该条继续）。"""


def is_sync_package(frame: Any) -> bool:
    """判断是否为同步包（含 body.syncPushPackage.data 且非空）。"""
    if not isinstance(frame, dict):
        return False
    body = frame.get("body")
    if not isinstance(body, dict):
        return False
    pkg = body.get("syncPushPackage")
    if not isinstance(pkg, dict):
        return False
    data = pkg.get("data")
    return isinstance(data, list) and len(data) > 0


def decode_sync_packages(frame: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解码同步包内所有条目，返回 [{"index": i, "decoded": {...}}, ...]。

    单条失败不中断，错误条目标记 error 后跳过。
    """
    results: List[Dict[str, Any]] = []
    data_list = frame["body"]["syncPushPackage"]["data"]
    for idx, item in enumerate(data_list):
        if not isinstance(item, dict) or "data" not in item:
            continue
        try:
            decoded = _decode_item(item["data"])
            results.append({"index": idx, "decoded": decoded})
        except DecodeError as exc:
            results.append({"index": idx, "error": str(exc)})
    return results


def _decode_item(raw: str) -> Dict[str, Any]:
    """解码单条 data：base64 → JSON 明文或 MessagePack/加密。"""
    if not isinstance(raw, str):
        raise DecodeError("data 非字符串")

    try:
        raw_bytes = base64.b64decode(raw, validate=False)
    except Exception as exc:
        raise DecodeError(f"base64 解码失败: {exc}") from exc

    # 尝试明文 JSON
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 尝试 MessagePack / 加密
    try:
        decrypted_json = decrypt(raw)
        parsed = json.loads(decrypted_json)
        if isinstance(parsed, dict):
            return parsed
        raise DecodeError(f"解密结果非对象: {type(parsed).__name__}")
    except json.JSONDecodeError as exc:
        raise DecodeError(f"解密结果非 JSON: {exc}") from exc
