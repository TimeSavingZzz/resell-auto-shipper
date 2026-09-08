"""运维告警推送（微信 / 邮件 / 日志）。

主渠道由 ALERT_CHANNEL 单选：log / serverchan / pushplus。
邮件为并列附加渠道：配置齐 ALERT_SMTP_HOST/PORT/USER/PASS + ALERT_EMAIL_TO 后，
无论主渠道是哪个（含 log），告警都会额外发一封邮件；不配置则不发邮件。

邮件键：
- ALERT_SMTP_HOST/PORT   默认 smtp.qq.com:465（465 走 SSL；587 走 STARTTLS）
- ALERT_SMTP_USER/PASS   SMTP 账号 / 授权码（不是登录密码）
- ALERT_EMAIL_TO         收件邮箱（空 = 不启用邮件）

notify(kind, title, text)：同一 kind 在 ALERT_THROTTLE（秒，默认 600）内只推一次，
避免 cookie 失效/风控等反复触发刷屏。线程安全（短连接，同步返回）。

约定：绝不发送 cookie/token 明文。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Dict, Optional

import requests

DEFAULT_CHANNEL = "log"
DEFAULT_THROTTLE = 600


def _log() -> object:
    from loguru import logger

    return logger


class Alerter:
    def __init__(
        self,
        *,
        channel: Optional[str] = None,
        sendkey: Optional[str] = None,
        pushplus_token: Optional[str] = None,
        throttle: Optional[float] = None,
        email_to: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_pass: Optional[str] = None,
    ) -> None:
        self.channel = (channel or os.getenv("ALERT_CHANNEL", DEFAULT_CHANNEL)).strip().lower()
        if self.channel not in ("log", "serverchan", "pushplus"):
            self.channel = "log"
        self.sendkey = str(sendkey if sendkey is not None else os.getenv("ALERT_SENDKEY", "")).strip()
        self.pushplus_token = str(
            pushplus_token if pushplus_token is not None else os.getenv("ALERT_PUSHPLUS_TOKEN", "")
        ).strip()
        self.email_to = str(email_to if email_to is not None else os.getenv("ALERT_EMAIL_TO", "")).strip()
        self.smtp_host = str(
            smtp_host if smtp_host is not None else os.getenv("ALERT_SMTP_HOST", "smtp.qq.com")
        ).strip()
        try:
            self.smtp_port = int(
                smtp_port if smtp_port is not None else os.getenv("ALERT_SMTP_PORT", "465")
            )
        except (TypeError, ValueError):
            self.smtp_port = 465
        self.smtp_user = str(
            smtp_user if smtp_user is not None else os.getenv("ALERT_SMTP_USER", "")
        ).strip()
        self.smtp_pass = str(
            smtp_pass if smtp_pass is not None else os.getenv("ALERT_SMTP_PASS", "")
        ).strip()
        try:
            self.throttle = float(
                throttle if throttle is not None else os.getenv("ALERT_THROTTLE", str(DEFAULT_THROTTLE))
            )
        except (TypeError, ValueError):
            self.throttle = DEFAULT_THROTTLE
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        if self.channel == "log":
            return True
        return bool(self._push_targets())

    @property
    def _email_ok(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.email_to)

    def _push_targets(self) -> list:
        """当前实际会外推的渠道：主渠道(serverchan/pushplus) + 附加 email。"""
        targets: list = []
        if self.channel == "serverchan" and self.sendkey:
            targets.append("serverchan")
        elif self.channel == "pushplus" and self.pushplus_token:
            targets.append("pushplus")
        if self._email_ok:
            targets.append("email")
        return targets

    def _throttled(self, kind: str) -> bool:
        """True=允许推送。首次允许；同 kind 在窗口内被抑制。"""
        with self._lock:
            now = time.time()
            last = self._last.get(kind, 0.0)
            if now - last < self.throttle:
                return False
            self._last[kind] = now
            return True

    def notify(self, kind: str, title: str, text: str = "") -> None:
        """推送一条告警/通知。kind 用于节流去重，用稳定分类词而非消息原文。"""
        try:
            self._notify(kind, title, text)
        except Exception as exc:  # noqa: BLE001  推送自身失败不应击穿业务
            _log().warning(f"告警推送失败 kind={kind} error={type(exc).__name__}: {exc}")

    def _notify(self, kind: str, title: str, text: str) -> None:
        targets = self._push_targets()
        if self.channel == "log":
            _log().warning(f"[alert:{kind}] {title} {text}".strip())
        if not targets:
            if self.channel != "log":
                _log().info(f"[alert:{kind}] (channel={self.channel} 未配置，仅记录) {title} {text}")
            return
        if not self._throttled(kind):
            _log().info(f"[alert:{kind}] 已节流抑制重复推送")
            return
        sent = []
        for target in targets:
            try:
                getattr(self, f"_push_{target}")(title, text)
                sent.append(target)
            except Exception as exc:  # noqa: BLE001  单个渠道失败不拖累其它渠道
                _log().warning(
                    f"告警推送失败 target={target} kind={kind} error={type(exc).__name__}: {exc}"
                )
        if sent:
            _log().info(f"[alert:{kind}] 已推送 {','.join(sent)}: {title}")

    # ---- 渠道 ----

    def _push_serverchan(self, title: str, text: str) -> None:
        resp = requests.get(
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            params={"title": title[:255], "desp": text},
            timeout=10,
        )
        resp.raise_for_status()

    def _push_pushplus(self, title: str, text: str) -> None:
        resp = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": self.pushplus_token, "title": title[:255], "content": text},
            timeout=10,
        )
        resp.raise_for_status()

    def _push_email(self, title: str, text: str) -> None:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        body = MIMEText((text or title)[:4000], "plain", "utf-8")
        body["Subject"] = Header(title[:120], "utf-8")
        body["From"] = self.smtp_user
        body["To"] = self.email_to
        if self.smtp_port == 587:
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port or 465, timeout=15)
        try:
            smtp.login(self.smtp_user, self.smtp_pass)
            smtp.sendmail(self.smtp_user, [self.email_to], body.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass


_alerter_instance: Optional[Alerter] = None
_alerter_lock = threading.Lock()


def get_alerter() -> Alerter:
    """模块级单例（惰性，读取一次环境变量）。"""
    global _alerter_instance
    if _alerter_instance is None:
        with _alerter_lock:
            if _alerter_instance is None:
                _alerter_instance = Alerter()
    return _alerter_instance


def notify(kind: str, title: str, text: str = "") -> None:
    """便捷入口：get_alerter().notify(kind, title, text)。"""
    get_alerter().notify(kind, title, text)


async def anotify(kind: str, title: str, text: str = "") -> None:
    """异步入口：在线程池执行 notify，避免阻塞事件循环（requests 短连接）。"""
    await asyncio.to_thread(notify, kind, title, text)
