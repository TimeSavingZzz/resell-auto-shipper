"""测试用 MessagePack 编码器（仅用于构造模拟帧，生产代码使用 app.crypto.decrypt）。"""
from __future__ import annotations

import struct
from typing import Any


def pack(value: Any) -> bytes:
    if value is None:
        return b"\xc0"
    if value is False:
        return b"\xc2"
    if value is True:
        return b"\xc3"
    if isinstance(value, int):
        if 0 <= value <= 127:
            return bytes([value])
        if 128 <= value <= 255:
            return b"\xcc" + struct.pack(">B", value)
        if 256 <= value <= 65535:
            return b"\xcd" + struct.pack(">H", value)
        if 0 <= value <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", value)
        return b"\xcf" + struct.pack(">Q", value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        length = len(data)
        if length <= 31:
            return bytes([0xA0 | length]) + data
        if length <= 255:
            return b"\xd9" + struct.pack(">B", length) + data
        return b"\xda" + struct.pack(">H", length) + data
    if isinstance(value, (list, tuple)):
        length = len(value)
        if length <= 15:
            head = bytes([0x90 | length])
        else:
            head = b"\xdc" + struct.pack(">H", length)
        return head + b"".join(pack(v) for v in value)
    if isinstance(value, dict):
        length = len(value)
        if length <= 15:
            head = bytes([0x80 | length])
        else:
            head = b"\xde" + struct.pack(">H", length)
        body = b""
        for k, v in value.items():
            body += pack(k) + pack(v)
        return head + body
    raise TypeError(f"unsupported type: {type(value)}")
