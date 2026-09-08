"""闲鱼 IM 协议工具函数。

实现严格依据 Phase 0 源码研究结果（shaxiu/XianyuAutoAgent 与
xianyu-auto-agent-local-console 的 utils/xianyu_utils.py 为最终实现依据，
二者在该协议部分实现完全一致）。

模块内全部为无副作用的纯函数：ID 生成、签名、cookie 解析、MessagePack 解码、解密。
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import struct
import time
from typing import Any, Dict

# 与参考项目一致的常量
SIGN_APP_KEY = "34839810"  # mtop 通用 appKey（用于 sign）
REG_APP_KEY = "444e9908a51d1cb236a27862abc769c9"  # /reg 使用的 app-key
REG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) "
    "DingWeb/2.1.5 IMPaaS DingWeb/2.1.5"
)


def trans_cookies(cookies_str: str) -> Dict[str, str]:
    """解析 cookie 字符串为字典（分号分隔、等号切分）。"""
    cookies: Dict[str, str] = {}
    for cookie in cookies_str.split(";"):
        if "=" not in cookie:
            continue
        key, _, value = cookie.strip().partition("=")
        if key:
            cookies[key] = value
    return cookies


def generate_mid() -> str:
    """生成消息 ID（mid），格式：{随机数}{时间戳毫秒} 0。"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def generate_uuid() -> str:
    """生成 uuid，格式：-{时间戳毫秒}1。"""
    timestamp = int(time.time() * 1000)
    return f"-{timestamp}1"


def generate_device_id(user_id: str) -> str:
    """基于用户 ID 生成 36 位设备 ID（UUID v4 外形）。"""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result: list[str] = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            result.append("-")
        elif i == 14:
            result.append("4")
        elif i == 19:
            rand_val = int(16 * random.random())
            result.append(chars[(rand_val & 0x3) | 0x8])
        else:
            rand_val = int(16 * random.random())
            result.append(chars[rand_val])
    return "".join(result) + "-" + user_id


def generate_sign(t: str, token: str, data: str) -> str:
    """生成 mtop 签名：MD5(token & t & appKey & data)。"""
    msg = f"{token}&{t}&{SIGN_APP_KEY}&{data}"
    md5_hash = hashlib.md5()
    md5_hash.update(msg.encode("utf-8"))
    return md5_hash.hexdigest()


class MessagePackDecoder:
    """MessagePack 纯 Python 解码器（参考项目自带实现，避免引入额外依赖）。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.length = len(data)

    def _read(self, count: int) -> bytes:
        if self.pos + count > self.length:
            raise ValueError("Unexpected end of MessagePack data")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def _read_u(self, fmt: str) -> int:
        return struct.unpack(fmt, self._read(struct.calcsize(fmt)))[0]

    def _read_str(self, length: int) -> str:
        return self._read(length).decode("utf-8")

    def decode(self) -> Any:
        return self._decode_value()

    def _decode_value(self) -> Any:
        if self.pos >= self.length:
            raise ValueError("Unexpected end of MessagePack data")
        b = self.data[self.pos]
        self.pos += 1

        if b <= 0x7F:
            return b  # positive fixint
        if 0x80 <= b <= 0x8F:
            return self._decode_map(b & 0x0F)
        if 0x90 <= b <= 0x9F:
            return self._decode_array(b & 0x0F)
        if 0xA0 <= b <= 0xBF:
            return self._read_str(b & 0x1F)
        if b == 0xC0:
            return None
        if b == 0xC2:
            return False
        if b == 0xC3:
            return True
        if b == 0xC4:
            return self._read(self._read_u(">B"))
        if b == 0xC5:
            return self._read(self._read_u(">H"))
        if b == 0xC6:
            return self._read(self._read_u(">I"))
        if b == 0xCA:
            return struct.unpack(">f", self._read(4))[0]
        if b == 0xCB:
            return struct.unpack(">d", self._read(8))[0]
        if b == 0xCC:
            return self._read_u(">B")
        if b == 0xCD:
            return self._read_u(">H")
        if b == 0xCE:
            return self._read_u(">I")
        if b == 0xCF:
            return self._read_u(">Q")
        if b == 0xD0:
            return self._read_u(">b")
        if b == 0xD1:
            return self._read_u(">h")
        if b == 0xD2:
            return self._read_u(">i")
        if b == 0xD3:
            return self._read_u(">q")
        if b == 0xD9:
            return self._read_str(self._read_u(">B"))
        if b == 0xDA:
            return self._read_str(self._read_u(">H"))
        if b == 0xDB:
            return self._read_str(self._read_u(">I"))
        if b == 0xDC:
            return self._decode_array(self._read_u(">H"))
        if b == 0xDD:
            return self._decode_array(self._read_u(">I"))
        if b == 0xDE:
            return self._decode_map(self._read_u(">H"))
        if b == 0xDF:
            return self._decode_map(self._read_u(">I"))
        if b >= 0xE0:
            return b - 256  # negative fixint
        raise ValueError(f"Unknown MessagePack format byte: 0x{b:02x}")

    def _decode_array(self, size: int) -> list:
        return [self._decode_value() for _ in range(size)]

    def _decode_map(self, size: int) -> dict:
        result: dict = {}
        for _ in range(size):
            key = self._decode_value()
            value = self._decode_value()
            result[key] = value
        return result


def decrypt(data: str) -> str:
    """解密 WebSocket 同步包 data 字段。

    流程（与参考项目一致）：base64 解码 → MessagePack 解码 → JSON 序列化。
    任一步失败时降级尝试 UTF-8 文本，最终兜底返回十六进制。
    """
    cleaned = "".join(c for c in data if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    while len(cleaned) % 4 != 0:
        cleaned += "="
    try:
        decoded_bytes = base64.b64decode(cleaned)
    except Exception:
        return json.dumps({"decode_error": "base64 decode failed", "raw_data": data}, ensure_ascii=False)

    try:
        result = MessagePackDecoder(decoded_bytes).decode()

        def _json_default(obj: Any) -> str:
            if isinstance(obj, bytes):
                try:
                    return obj.decode("utf-8")
                except Exception:
                    return base64.b64encode(obj).decode("utf-8")
            return str(obj)

        return json.dumps(result, ensure_ascii=False, default=_json_default)
    except Exception as msgpack_err:
        try:
            text_result = decoded_bytes.decode("utf-8")
            return json.dumps({"text": text_result}, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {"hex": decoded_bytes.hex(), "decode_error": f"msgpack failed: {msgpack_err}"},
                ensure_ascii=False,
            )
