"""分享链接/纯数字 → item_id 解析的离线测试。"""
from __future__ import annotations

from app.links import extract_item_id, extract_item_id_from_page, looks_like_short_link, resolve_item_id


def test_bare_digits():
    assert extract_item_id("2000000000000") == "2000000000000"


def test_bare_digits_wrong_length():
    assert extract_item_id("12345") is None          # 太短
    assert extract_item_id("12345678901234567890") is None  # 太长（订单/多 id）


def test_goofish_query_id():
    assert extract_item_id("https://www.goofish.com/item?id=6941234567890&spm=xx") == "6941234567890"


def test_goofish_path_id():
    assert extract_item_id("https://www.goofish.com/item/6941234567890.html") == "6941234567890"


def test_taobao_query_id():
    assert extract_item_id("https://item.taobao.com/item.htm?id=2000000000000&foo=1") == "2000000000000"


def test_share_copy_text_has_digit_run():
    # app 复制口令里夹着一串数字 id
    assert extract_item_id("7XElLdgAw 复制打开闲鱼 6941234567890 打开看看") == "6941234567890"


def test_empty_and_none():
    assert extract_item_id("") is None
    assert extract_item_id(None) is None


def test_short_link_needs_resolver():
    text = "8XoQ2fgH:/ 复制打开闲鱼 https://m.tb.cn/h.gQ12xE?tk=abcd"
    assert extract_item_id(text) is None
    assert looks_like_short_link(text) is True
    # 无 resolver 不触网 → None
    assert resolve_item_id(text) is None


def test_short_link_resolved_via_redirect():
    text = "https://m.tb.cn/h.gQ12xE?tk=abcd"
    resolver = lambda url: "https://www.goofish.com/item?id=6941234567890"  # noqa: E731
    assert resolve_item_id(text, resolver=resolver) == "6941234567890"


def test_short_link_landing_page_contains_id():
    # tb.cn 分享短链不 302，真实地址内嵌在落地页 HTML 里
    text = "https://m.tb.cn/h.8KQtNzr?tk=abc"
    landing = (
        '<html>...window.flowUrl="https://h5.m.goofish.com/item?forceFlush=1'
        "&amp;itemId=1000000000001&amp;id=1000000000001\"...</html>"
    )
    assert extract_item_id_from_page(landing) == "1000000000001"
    assert resolve_item_id(text, resolver=lambda url: landing) == "1000000000001"


def test_from_page_ignores_unrelated_numbers():
    # 页面上其它 10~15 位数字（如 trid/user 号段）不应被当 item
    landing = 'var trid="215041f717888614089881682e"; user="1234567890"; x="9";'
    assert extract_item_id_from_page(landing) is None
    # userId/ownerId 尾缀的 "Id" 不是 item 的 id 参数
    assert extract_item_id_from_page("?ownerId=1234567890&ut_sk=x&category=") is None
    # 同一串里 itemId 更优先（不因 ownerId 先出现而误取）
    assert extract_item_id_from_page("?userId=1234567890&itemId=1000000000001") == "1000000000001"


def test_resolver_exception_swallowed():
    def boom(url):
        raise RuntimeError("网络失败")

    text = "https://m.tb.cn/h.gQ12xE?tk=abcd"
    assert resolve_item_id(text, resolver=boom) is None
