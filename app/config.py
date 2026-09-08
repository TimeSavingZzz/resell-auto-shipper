"""只读监听器的本地配置。

只负责从 .env 或 config.json 读取认证信息与运行参数。
不包含任何写操作 / 登录流程。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

from .crypto import trans_cookies

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


class Config:
    def __init__(self) -> None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)

        self.base_url = "wss://wss-goofish.dingtalk.com/"
        self.heartbeat_interval = float(os.getenv("HEARTBEAT_INTERVAL", "15"))
        self.heartbeat_timeout = float(os.getenv("HEARTBEAT_TIMEOUT", "30"))
        self.max_backoff = float(os.getenv("MAX_BACKOFF", "60"))
        self.log_dir = PROJECT_ROOT / "logs" / "raw_events"
        self.fixture_dir = PROJECT_ROOT / "tests" / "fixtures" / "live_events"

        self._cookies_str = ""
        self._token = (os.getenv("TOKEN") or "").strip()

        env_cookies = (os.getenv("COOKIES_STR") or "").strip()
        if env_cookies and env_cookies != "your_cookies_here":
            self._cookies_str = env_cookies
        else:
            self._load_from_config_json()

    def _load_from_config_json(self) -> None:
        cfg_path = PROJECT_ROOT / "config.json"
        if not cfg_path.exists():
            return
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = data.get("cookies", "")
        token = data.get("token", "")
        if cookies:
            self._cookies_str = cookies
        if token:
            self._token = token

    @property
    def cookies_str(self) -> str:
        return self._cookies_str

    @property
    def has_auth(self) -> bool:
        return bool(self._cookies_str) or bool(self._token)

    @property
    def auth_ok(self) -> bool:
        """布尔鉴权（供 Web 端复用，不抛 SystemExit）。"""
        return self.has_auth

    def set_cookie(self, new_cookie: str) -> None:
        """更新 cookie：写 .env（持久）+ 内存。进程内运行中的快照需另行刷新。"""
        new_cookie = (new_cookie or "").strip()
        if new_cookie and new_cookie != "your_cookies_here":
            self._cookies_str = new_cookie
            try:
                set_key(ENV_PATH, "COOKIES_STR", new_cookie)
            except OSError:
                pass

    def clear_cookie(self) -> None:
        self._cookies_str = ""

    @property
    def myid(self) -> str:
        """账号 unb：从 Cookie 动态提取（换 cookie 后自动跟随）。"""
        return trans_cookies(self._cookies_str).get("unb", "")

    def require_auth(self) -> None:
        if not self.has_auth:
            raise SystemExit(
                "未找到认证信息。请在项目根目录的 .env 中配置 COOKIES_STR，"
                "或创建 config.json 并写入 cookies 字段。"
            )


config = Config()
