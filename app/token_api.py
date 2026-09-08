"""只读 token 获取。

调用 mtop.taobao.idlemessage.pc.login.token 用 Cookie 换取 WebSocket 注册
所需的 accessToken。此接口仅返回凭证，不发送消息、不修改任何数据，是
连接监听器的必要认证步骤。

与参考项目实现的差异：
- 不写回 .env / 不清洗 session cookies（无副作用）
- 不弹窗要求人工输入新 Cookie（失败直接抛异常）
- 不做无限重试（仅允许一次失败后原样抛出，由上层决定退出策略）
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests
from loguru import logger

from .crypto import generate_device_id, generate_sign


class TokenFetchError(Exception):
    """token 获取失败（含 Cookie 失效 / 风控等）。"""


_TOKEN_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"


def _cookie_str_from_dict(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _round(cookie_str: str, device_id: str, timeout: float = 20.0) -> Dict[str, Any]:
    """发起一轮 token API 请求。返回 ret / data / 会话内全部 cookie。

    请求语义（headers/params）与参考项目 probe_cookie_verification_from_cookie
    完全一致，含浏览器 sec-* 头，避免被 mtop 风控识别为非浏览器请求。
    """
    cookies = _parse_cookies(cookie_str)
    session = requests.Session()
    session.headers.update(
        {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "origin": "https://www.goofish.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.goofish.com/",
            "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
        }
    )
    # 与参考项目一致：cookies.update() 注入全部 cookie（cookie 不带 domain）
    session.cookies.update(cookies)

    token_raw = cookies.get("_m_h5_tk", "").split("_")[0]
    t = str(int(time.time()) * 1000)  # 与参考项目一致：整秒×1000
    data_val = (
        '{"appKey":"444e9908a51d1cb236a27862abc769c9",'
        f'"deviceId":"{device_id}"'
        "}"
    )
    params = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": t,
        "sign": generate_sign(t, token_raw, data_val),
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": "mtop.taobao.idlemessage.pc.login.token",
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": "a21ybx.im.0.0",
    }

    resp = session.post(_TOKEN_API_URL, params=params, data={"data": data_val}, timeout=timeout)
    try:
        body: Dict[str, Any] = resp.json()
    except Exception:
        raise TokenFetchError(f"token 响应非 JSON: HTTP status={resp.status_code}") from None
    try:
        session_cookies = dict(session.cookies.get_dict())
    except Exception:
        session_cookies = dict(cookies)

    return {
        "ret": body.get("ret") or [],
        "data": body.get("data") or {},
        "session_cookies": session_cookies,
        "http_status": resp.status_code,
    }


def fetch_access_token(
    cookies_str: str,
    myid: str,
    device_id: Optional[str] = None,
    timeout: float = 20.0,
) -> str:
    """用 Cookie 换取 WebSocket accessToken，失败抛 TokenFetchError。

    device_id 必须与后续 /reg 的 did 一致（服务端校验二者相等），
    否则返回 401 "device id or appkey is not equal"。

    令牌引导：_m_h5_tk 过期（FAIL_SYS_TOKEN_EXOIRED/EXPIRED，可恢复）时，
    用本轮响应 Set-Cookie 之后的完整 cookie 串发起新一轮请求（stale-token
    自愈，与参考探针 probe_cookie_status.py 一致）；仍失败则抛异常。
    """
    if device_id is None:
        device_id = generate_device_id(myid or "unknown")

    current_cookies = cookies_str
    for attempt in range(2):
        result = _round(current_cookies, device_id, timeout=timeout)
        ret = result["ret"]
        ret_str = str(ret)

        if any("SUCCESS::调用成功" in r for r in ret):
            token = result["data"].get("accessToken")
            if not token:
                raise TokenFetchError("token 响应成功但缺少 accessToken")
            logger.info(f"token API 成功: HTTP status={result['http_status']}, mtop ret={ret}")
            return token

        token_expired = any(
            k in ret_str for k in ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED")
        )
        if token_expired and attempt == 0:
            refreshed = _cookie_str_from_dict(result["session_cookies"])
            if refreshed and refreshed != current_cookies:
                logger.info("_m_h5_tk 过期，已用响应新 cookie 重试")
                current_cookies = refreshed
                continue

        if "RGV587_ERROR" in ret_str or "被挤爆啦" in ret_str:
            raise TokenFetchError(
                f"触发风控/限流（RGV587_ERROR）: HTTP status={result['http_status']}, mtop ret={ret_str}"
            )
        raise TokenFetchError(
            f"token 接口返回失败: HTTP status={result['http_status']}, mtop ret={ret_str}"
        )

    raise TokenFetchError("token 获取失败：连续两轮令牌过期")


def _parse_cookies(cookies_str: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in cookies_str.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.strip().partition("=")
        if key:
            result[key] = value
    return result
