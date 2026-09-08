import base64
import hashlib

from app.crypto import (
    MessagePackDecoder,
    decrypt,
    generate_mid,
    generate_sign,
    trans_cookies,
)
from tests.msgpack_helper import pack


def test_trans_cookies():
    cookies = trans_cookies("a=1; b=2; unb=12345")
    assert cookies == {"a": "1", "b": "2", "unb": "12345"}


def test_generate_sign_matches_reference():
    # 参考实现: md5(token & t & appKey & data)，appKey=34839810
    t = "1700000000000"
    token = "abc123"
    data = '{"a":1}'
    expected = hashlib.md5(f"{token}&{t}&34839810&{data}".encode("utf-8")).hexdigest()
    assert generate_sign(t, token, data) == expected


def test_generate_mid_shape():
    mid = generate_mid()
    assert " " in mid
    head, tail = mid.split(" ", 1)
    assert head.isdigit()
    assert tail == "0"


def test_messagepack_roundtrip():
    original = {"1": "56226853668@goofish", "2": 1, "3": {"redReminder": "等待卖家发货", "userId": "2000123456"}}
    encoded = pack(original)
    b64 = base64.b64encode(encoded).decode("utf-8")
    result = decrypt(b64)
    import json

    assert json.loads(result) == original


def test_messagepack_decoder_ints():
    data = bytes([0xCE]) + (250000000).to_bytes(4, "big")
    assert MessagePackDecoder(data).decode() == 250000000
    assert MessagePackDecoder(b"\x2a").decode() == 42
    assert MessagePackDecoder(b"\xe0").decode() == -32
