"""mtop 请求封装（签名 + stale-token 自愈）。

浏览器 sec-* 头 + 表单 data 上传 + MD5 签名，严格镜像参考项目 A2
（item_publisher / xianyu_utils / ship_api）已验证的参数。每次响应吸收
Set-Cookie 里的新 _m_h5_tk 供下一次请求签名与请求头使用；_m_h5_tk 过期
（FAIL_SYS_TOKEN_EXOIRED，可恢复）时用新 cookie 重试一次，其余失败不
无限重试，直接抛 MtopError。

仅供发布/详情/列表等 h5 mtop 写读接口复用。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

import requests
from loguru import logger

from .crypto import SIGN_APP_KEY, generate_sign, trans_cookies


class MtopError(Exception):
    """mtop 请求失败（会话过期 / 风控 / 业务错误等，均不可无限重试）。"""


def _http_headers() -> Dict[str, str]:
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


class Mtop:
    def __init__(self, cookies_str: str, timeout: float = 30.0) -> None:
        self.jar = trans_cookies(cookies_str)
        if not (self.jar.get("_m_h5_tk") or "").split("_")[0]:
            raise MtopError("cookies 中缺少 _m_h5_tk，无法生成签名")
        self.timeout = timeout
        self._cookie_str = cookies_str

    def post(
        self,
        *,
        api: str,
        version: str,
        data: Any,
        spm_cnt: str = "a21ybx.im.0.0",
        spm_pre: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """发起 mtop POST。成功（ret 含 SUCCESS::）返回整包 JSON，否则抛 MtopError。"""
        data_val = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

        last_err = ""
        for attempt in range(2):
            session = requests.Session()
            session.headers.update(_http_headers())
            session.headers["content-type"] = "application/x-www-form-urlencoded"
            if extra_headers:
                session.headers.update(extra_headers)
            session.cookies.update(self.jar)

            t = str(int(time.time()) * 1000)
            token = (self.jar.get("_m_h5_tk", "") or "").split("_")[0]
            params = {
                "jsv": "2.7.2",
                "appKey": SIGN_APP_KEY,
                "t": t,
                "sign": generate_sign(t, token, data_val),
                "v": version,
                "type": "originaljson",
                "accountSite": "xianyu",
                "dataType": "json",
                "timeout": "20000",
                "api": api,
                "sessionOption": "AutoLoginOnly",
                "spm_cnt": spm_cnt,
            }
            if spm_pre:
                params["spm_pre"] = spm_pre
            params["log_id"] = f"m{int(time.time() * 1000)}"

            url = f"https://h5api.m.goofish.com/h5/{api}/{version}/"
            try:
                resp = session.post(
                    url, params=params, data={"data": data_val}, timeout=self.timeout
                )
                body = resp.json()
            except requests.RequestException as exc:
                raise MtopError(f"{api} 网络异常: {exc}") from exc
            except ValueError:
                raise MtopError(
                    f"{api} 响应非 JSON: HTTP status={getattr(resp, 'status_code', '?')}"
                ) from None
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            try:
                for c in resp.cookies:
                    if c.name and c.value:
                        self.jar[c.name] = c.value
            except Exception:
                pass

            ret = body.get("ret") or []
            ret_str = str(ret)
            if any("SUCCESS::" in str(r) for r in ret):
                return body

            last_err = ret_str
            token_expired = any(
                k in ret_str for k in ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED")
            )
            if token_expired and attempt == 0:
                logger.info(f"{api} _m_h5_tk 过期，已吸收响应 cookie 重试一次")
                continue
            if "RGV587_ERROR" in ret_str or "被挤爆啦" in ret_str or "ILLEGAL" in ret_str:
                break
            if "FAIL_SYS_SESSION_EXPIRED" in ret_str or "Session过期" in ret_str:
                break

        raise MtopError(f"{api} 失败: {last_err}")
