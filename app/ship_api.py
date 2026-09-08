"""真实发货确认：mtop.taobao.idle.logistic.consign.dummy（虚拟/无需物流发货）。

仅做"把已付款订单置为已发货"这一个 HTTP 写操作，参数与判定严格依据
参考项目 A2 已验证的实现（secure_confirm_decrypted.py），不猜测协议。

直连闲鱼，禁止走代理（v2ray 等仅限 GitHub）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict

import requests
from loguru import logger

from .crypto import SIGN_APP_KEY, trans_cookies

API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/"


class ShipConfirmError(Exception):
    """发货确认失败（会话过期 / 订单状态不正确等）。"""


def _sign(t: str, token: str, data: str) -> str:
    msg = f"{token}&{t}&{SIGN_APP_KEY}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


def _http_headers() -> Dict[str, str]:
    """浏览器风控头（与 token_api / 参考探针一致）。"""
    return {
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


def confirm_dummy_ship(
    cookies_str: str,
    order_id: str,
    timeout: float = 30.0,
    max_retry: int = 2,
) -> Dict[str, Any]:
    """调用虚拟发货接口，把 order_id 置为已发货。

    返回 {"success": True, "order_id": ...} 或抛 ShipConfirmError。

    每次响应都会把 Set-Cookie 更新的 cookie 吸收回来用于下一次签名与请求头
    （_m_h5_tk 过期属可恢复的 stale-token，与参考实现 auto_confirm 一致）。
    """
    jar = trans_cookies(cookies_str)
    if not (jar.get("_m_h5_tk") or "").split("_")[0]:
        raise ShipConfirmError("cookies 中缺少 _m_h5_tk，无法生成签名")

    data_val = (
        '{"orderId":"' + order_id + '", "tradeText":"","picList":[],"newUnconsign":true}'
    )

    last_err: str = ""
    for attempt in range(max_retry + 1):
        session = requests.Session()
        session.headers.update(_http_headers())
        session.headers["content-type"] = "application/x-www-form-urlencoded"
        session.cookies.update(jar)

        t = str(int(time.time()) * 1000)
        token = (jar.get("_m_h5_tk", "") or "").split("_")[0]
        params = {
            "jsv": "2.7.2",
            "appKey": SIGN_APP_KEY,
            "t": t,
            "sign": _sign(t, token, data_val),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idle.logistic.consign.dummy",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }
        try:
            resp = session.post(
                API_URL, params=params, data={"data": data_val}, timeout=timeout
            )
            body = resp.json()
        except requests.RequestException as exc:
            last_err = f"网络异常: {exc}"
        except ValueError:
            last_err = f"响应非 JSON: HTTP status={getattr(resp, 'status_code', '?')}"
        else:
            # 吸收响应 Set-Cookie（含新的 _m_h5_tk）供下一次请求使用
            try:
                for c in resp.cookies:
                    if c.name and c.value:
                        jar[c.name] = c.value
            except Exception:
                pass

            ret = body.get("ret") or []
            ret_str = str(ret)
            if any("SUCCESS::调用成功" in str(r) for r in ret):
                logger.info(f"发货确认成功: order_id={order_id}")
                return {"success": True, "order_id": order_id}

            last_err = ret_str
            if "FAIL_SYS_SESSION_EXPIRED" in ret_str or "Session过期" in ret_str:
                raise ShipConfirmError(f"会话过期（需重新登录）: {last_err}")
            if "ORDER_STATUS_ERROR" in ret_str or "订单状态不正确" in ret_str:
                raise ShipConfirmError(f"订单状态不正确（不可重试）: {last_err}")

        if attempt < max_retry:
            time.sleep(0.5)

    raise ShipConfirmError(f"发货确认失败: {last_err}")
