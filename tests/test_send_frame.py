"""发送帧构造测试（完全离线，无网络）。格式须与参考项目 A2 的 send_msg 一致。"""
from __future__ import annotations

import base64
import json

from app.protocol import build_send_message_frame


def test_send_frame_lwp_and_receivers():
    frame = build_send_message_frame(buyer_id="2000000000001", own_id="100000000", text="您好")
    assert frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
    body = frame["body"]
    assert body[0]["cid"] == "2000000000001@goofish"
    assert body[0]["conversationType"] == 1
    assert body[1]["actualReceivers"] == ["2000000000001@goofish", "100000000@goofish"]


def test_send_frame_content_is_base64_custom_text():
    frame = build_send_message_frame(buyer_id="2000000000001", own_id="100000000", text="资料：https://example.com/x\n提取码 8888")
    content = frame["body"][0]["content"]
    assert content["contentType"] == 101
    raw = base64.b64decode(content["custom"]["data"])
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded["contentType"] == 1
    assert decoded["text"]["text"] == "资料：https://example.com/x\n提取码 8888"


def test_send_frame_has_uuid_mid_extensions():
    frame = build_send_message_frame(buyer_id="1", own_id="2", text="x")
    entry = frame["body"][0]
    assert frame["headers"]["mid"]
    assert entry["uuid"]
    assert entry["extension"] == {"extJson": "{}"}
    assert entry["ctx"] == {"appVersion": "1.0", "platform": "web"}
    assert entry["msgReadStatusSetting"] == 1
