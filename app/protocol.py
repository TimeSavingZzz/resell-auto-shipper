"""WebSocket 协议帧构造（只读）。

帧格式与 /reg / ackDiff / 心跳 / ACK 均严格依据 Phase 0 源码研究确认的
实现，不重新猜测协议。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, Optional

from .crypto import REG_APP_KEY, REG_UA, generate_mid, generate_uuid


def build_reg_frame(token: str, device_id: str, myid: str) -> Dict[str, Any]:
    """注册帧：lwp=/reg，携带 app-key / token / 设备信息。"""
    return {
        "lwp": "/reg",
        "headers": {
            "cache-header": "app-key token ua wv",
            "app-key": REG_APP_KEY,
            "token": token,
            "ua": REG_UA,
            "dt": "j",
            "wv": "im:3,au:3,sy:6",
            "sync": "0,0;0;0;",
            "did": device_id,
            "mid": generate_mid(),
        },
    }


def build_sync_ack_frame() -> Dict[str, Any]:
    """状态同步帧：lwp=/r/SyncStatus/ackDiff，上报 sync 游标。"""
    now_ms = int(time.time() * 1000)
    return {
        "lwp": "/r/SyncStatus/ackDiff",
        "headers": {"mid": "5701741704675979 0"},
        "body": [
            {
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": now_ms * 1000,
                "seq": 0,
                "timestamp": now_ms,
            }
        ],
    }


def build_heartbeat_frame() -> Dict[str, Any]:
    """心跳帧：lwp=/!。"""
    return {"lwp": "/!", "headers": {"mid": generate_mid()}}


def build_ack_frame(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """构造对收到的帧的 ACK 确认。

    参考实现：仅当帧带 headers.mid 时确认，并回传 mid/sid/app-key/ua/dt。
    """
    headers = message.get("headers") if isinstance(message, dict) else None
    if not isinstance(headers, dict) or "mid" not in headers:
        return None

    ack_headers: Dict[str, Any] = {
        "mid": headers["mid"],
        "sid": headers.get("sid", ""),
    }
    for key in ("app-key", "ua", "dt"):
        if key in headers:
            ack_headers[key] = headers[key]

    return {"code": 200, "headers": ack_headers}


def build_send_message_frame(buyer_id: str, own_id: str, text: str) -> Dict[str, Any]:
    """构造发送文本消息帧（lwp=/r/MessageSend/sendByReceiverScope）。

    帧格式严格依据参考项目 A2 的 send_msg 实现（XianyuAutoAsync.py:12839）。
    buyer_id 同时作为会话 cid 与接收方 toid（单聊会话 sid 前缀 = 对方用户 id）。
    """
    text_payload = {"contentType": 1, "text": {"text": text}}
    text_base64 = base64.b64encode(
        json.dumps(text_payload, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")

    return {
        "lwp": "/r/MessageSend/sendByReceiverScope",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "uuid": generate_uuid(),
                "cid": f"{buyer_id}@goofish",
                "conversationType": 1,
                "content": {
                    "contentType": 101,
                    "custom": {"type": 1, "data": text_base64},
                },
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{buyer_id}@goofish", f"{own_id}@goofish"]},
        ],
    }


def websocket_headers(cookies_str: str) -> Dict[str, str]:
    """WebSocket 握手 headers（与参考项目一致）。"""
    return {
        "Cookie": cookies_str,
        "Host": "wss-goofish.dingtalk.com",
        "Connection": "Upgrade",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.goofish.com",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
