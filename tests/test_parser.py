import base64
import json

from app.crypto import decrypt
from app.decoder import decode_sync_packages, is_sync_package
from app.parser import parse_event
from app.samples import redact
from tests.msgpack_helper import pack


def _sync_frame(decoded_message: dict):
    encoded = pack(decoded_message)
    b64 = base64.b64encode(encoded).decode("utf-8")
    return {
        "lwp": "push",
        "headers": {"mid": "100 0"},
        "body": {
            "syncPushPackage": {
                "data": [{"data": b64}],
            }
        },
    }


def test_is_sync_package():
    frame = _sync_frame({"1": "x"})
    assert is_sync_package(frame)
    assert not is_sync_package({"lwp": "x"})
    assert not is_sync_package({"body": {"syncPushPackage": {"data": []}}})


def test_decode_plain_json_branch():
    # 明文 JSON 分支：base64 解码后直接 JSON 解析（不等 MessagePack）
    payload = json.dumps({"hello": "world", "1": "123@goofish"}, ensure_ascii=False)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("utf-8")
    frame = {
        "headers": {"mid": "1 0"},
        "body": {"syncPushPackage": {"data": [{"data": b64}]}},
    }
    entries = decode_sync_packages(frame)
    assert entries[0]["decoded"]["hello"] == "world"


def test_decode_sync_packages_roundtrip():
    inner = {"1": "56226853668@goofish", "3": {"redReminder": "等待卖家发货", "userId": "2000123456"}}
    entries = decode_sync_packages(_sync_frame(inner))
    assert len(entries) == 1
    assert entries[0]["decoded"] == inner


def test_parse_waiting_ship_simplified():
    # 简化结构：message['1'] 为字符串（会话 sid）
    inner = {
        "1": "56226853668@goofish",
        "2": 1,
        "3": {"redReminder": "等待卖家发货", "userId": "2000123456", "needPush": "false"},
    }
    parsed = parse_event(inner, own_id="8888")
    assert parsed["redReminder"] == "等待卖家发货"
    assert parsed["event_type"] == "red_reminder"
    assert parsed["chat_id"] == "56226853668"
    assert "2000123456" in parsed["candidate_buyer_ids"]


def test_parse_chat_message_with_ids():
    inner = {
        "1": {
            "2": "56226853668@goofish",
            "5": 1700000000000,
            "10": {
                "reminderContent": "老板在吗",
                "senderUserId": "3000111222",
                "reminderUrl": "https://www.goofish.com/item?itemId=998877665544",
            },
        },
        "3": {"needPush": "false"},
    }
    parsed = parse_event(inner, own_id="8888")
    assert parsed["event_type"] == "chat"
    assert parsed["chat_id"] == "56226853668"
    assert parsed["sender_id"] == "3000111222"
    assert "998877665544" in parsed["candidate_item_ids"]
    assert "3000111222" in parsed["candidate_buyer_ids"]


def test_extract_multiple_order_id_candidates():
    inner = {
        "1": "56226853668@goofish",
        "3": {"redReminder": "等待卖家发货"},
        "extJson": '{"orderId":"12345678901234567890","bizOrderId":"99988877766655544433"}',
        "updateKey": "trade_1234567890123456",
    }
    parsed = parse_event(inner)
    ids = parsed["candidate_order_ids"]
    # 多个候选全部保留，不认定唯一值
    assert "12345678901234567890" in ids
    assert "99988877766655544433" in ids
    assert any(len(cand) >= 16 for cand in ids)


def test_redact_sensitive():
    sample = {
        "token": "secret-token",
        "tracknick": "真实昵称",
        "content": "电话13812345678 邮箱 a@b.com",
        "safe": "itemId=123",
    }
    out = redact(sample)
    assert out["token"] == "[REDACTED]"
    assert out["tracknick"] == "[REDACTED]"
    assert "138****0000" in out["content"]
    assert "[email]@[redacted]" in out["content"]
    assert out["safe"] == "itemId=123"


def test_decrypt_and_parse_full_pipeline():
    inner = {
        "1": "56226853668@goofish",
        "2": 1,
        "3": {"redReminder": "等待卖家发货", "userId": "2000123456"},
    }
    entries = decode_sync_packages(_sync_frame(inner))
    parsed = parse_event(entries[0]["decoded"])
    assert parsed["redReminder"] == "等待卖家发货"
