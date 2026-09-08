"""Web 管理页（独立进程，不占 bot 的长连接）。

职责：
- 增删改商品/话术/发布模板（enabled 开关即时生效：写入 products.json → 原子
  replace → 写 data/control.json 版本号，bot 低频轮询后热重载 catalog）。
- 更新 Cookie：写 .env + control.json，bot 检测到变化后热刷新并重连。
- 只读报表：在售份数 / 已售总数 / 最近补发；手动 backfill / 手动发货（写操作，
  均有二次确认）。触发动作复用 relist/autoship 的真实接口。

安全：登录密码 = .env 的 ADMIN_TOKEN（无数据库）。POST /api/login 密码正确后种
HttpOnly 会话 cookie（14 天有效）；鉴权同时接受该 cookie 或请求头 X-Admin-Token
（兼容脚本/测试/命令行）。POST /api/password 改密码并写回 .env、清空全部会话。
未配置 ADMIN_TOKEN 则一律拒绝。绑定默认 127.0.0.1:8788（CONTROL_HOST /
CONTROL_PORT 可改），建议放 nginx HTTPS 反向代理后访问，不开公网裸端口。

运行：python -m app.control
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from flask import Flask, Response, jsonify, request

from .config import PROJECT_ROOT

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---- 商品文件读写（兼容新 {products:{}} 与旧扁平两种格式） ----

def _read_products_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _entries_of(data: Dict[str, Any]) -> Dict[str, Any]:
    """返回条目容器（dict key→entry）。顶层可能是 {products:{}} 或直接扁平。"""
    wrapped = isinstance(data.get("products"), dict)
    container = data["products"] if wrapped else data
    return dict(container)


def _entry_to_row(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "key": str(entry.get("key") or key),
        "name": str(entry.get("name") or ""),
        "message": str(entry.get("message") or ""),
        "enabled": bool(entry.get("enabled", True)),
    }
    source = entry.get("source_item_id")
    if source:
        row["source_item_id"] = str(source)
    aliases = entry.get("aliases")
    if isinstance(aliases, list):
        row["aliases"] = [str(a) for a in aliases]
    if isinstance(entry.get("listing"), dict):
        row["listing"] = entry["listing"]
    return row


_ALLOWED_FIELDS = {"key", "name", "message", "enabled", "source_item_id", "aliases", "listing"}


class Context:
    """应用状态 + 依赖注入点（测试可换 products/db/control 路径与假 IO）。"""

    def __init__(
        self,
        *,
        products_path: Path,
        relist_db: Path,
        control_path: Path,
        deliveries_db: Path,
        cookie_get: Callable[[], str],
        cookie_set: Callable[[str], None],
        myid: Callable[[], str],
        admin_token: str,
        fakes: Optional[Dict[str, Callable]] = None,
    ) -> None:
        self.products_path = Path(products_path)
        self.relist_db = Path(relist_db)
        self.control_path = Path(control_path)
        self.deliveries_db = Path(deliveries_db)
        self.cookie_get = cookie_get
        self.cookie_set = cookie_set
        self.myid = myid
        self.admin_token = admin_token
        # 登录会话（内存，重启即失效需重登）：sid -> 过期时间戳
        self.sessions: Dict[str, float] = {}
        self.fakes: Dict[str, Callable] = fakes or {}

        self._catalog: Any = None
        self.products_version = self._load_version()

    # ---- 商品容器 ----

    def load_entries(self) -> Dict[str, Any]:
        return _entries_of(_read_products_file(self.products_path))

    def save_entries(self, entries: Dict[str, Any]) -> None:
        data = _read_products_file(self.products_path)
        if data and isinstance(data.get("products"), dict):
            data["products"] = entries
            out = data
        else:
            out = entries
        _atomic_write(self.products_path, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        if self._catalog is not None:
            self._catalog.reload()
        self._bump_products_version()

    # ---- catalog ----

    def catalog(self):
        if self._catalog is None:
            from .catalog import Catalog

            self.relist_db.parent.mkdir(parents=True, exist_ok=True)
            self._catalog = Catalog(
                products_path=self.products_path,
                db_path=self.relist_db,
                min_online=2,
            )
        return self._catalog

    def manager(self):
        from .relist import RepublishManager

        mgr = RepublishManager(
            cookies_str=self.cookie_get(),
            catalog=self.catalog(),
            myid=self.myid(),
        )
        for key in ("list_fn", "publish_fn"):
            if key in self.fakes:
                setattr(mgr, "_" + key, self.fakes[key])
        return mgr

    def find_product_by_item(self, item_id: str) -> Optional[str]:
        """该 item_id 当前已归属的 product_key（含停用/仅别名）；未登记返回 None。"""
        try:
            catalog = self.catalog()
        except Exception:  # noqa: BLE001
            return None
        record = catalog.resolve(item_id)
        if record is not None:
            return str(record["key"])
        for key in catalog.product_keys():
            if item_id in catalog.alias_ids(key):
                return key
        return None

    # ---- control.json 握手 ----

    def _load_version(self) -> int:
        try:
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
            return int(data.get("products_version") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def _bump_products_version(self) -> None:
        self.products_version += 1
        self.write_control()

    def write_control(self, *, cookie: Optional[str] = None) -> None:
        payload = {
            "cookie": self.cookie_get() if cookie is None else cookie,
            "products_version": self.products_version,
        }
        _atomic_write(self.control_path, json.dumps(payload, ensure_ascii=False))


# ---- 登记已发布商品：链接解析 / 只读抓详情 / 写库 ----

class _RegError(Exception):
    """登记流程的业务错误：带 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _first_url(text: str) -> str:
    """从分享文本里抽出第一个 http(s) 链接（用户常连【闲鱼】前缀一起粘贴）。"""
    import re

    m = re.search(r"https?://[^\s\u4e00-\u9fff，。！？「」】（）()<>]+", text or "", re.IGNORECASE)
    return m.group(0) if m else ""


def _resolve_id(ctx: Context, text: str) -> str:
    """分享链接/纯数字 → item_id。短链经 resolver 跟随重定向（可注入 fake）。"""
    from .links import resolve_item_id

    resolver = ctx.fakes.get("url_resolver")
    if resolver is None:
        from .relist import clear_proxy_env

        _MOBILE_UA = (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/116 Mobile Safari/537.36"
        )

        def _fetch_short(raw: str) -> str:
            """入参常是整段分享文本：先抽 URL，再跟随重定向；tb.cn 短链不 302 时返回落地页文本。"""
            import requests

            clear_proxy_env()
            url = _first_url(raw) or raw.strip()
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=(6, 20),
                headers={"User-Agent": _MOBILE_UA},
            )
            if resp.url != url:
                return str(resp.url)
            return resp.text

        resolver = _fetch_short
    # 落地页内容偶发不一致/网络抖动 → 多次重试，成功一次即返回
    for _attempt in range(3):
        item_id = resolve_item_id(text, resolver=resolver)
        if item_id:
            return item_id
        time.sleep(0.4)
    raise _RegError(
        "未能从输入解析出 item_id：请粘贴闲鱼商品分享链接，或直接填 10~15 位商品数字 id"
        "（短链若需联网解析，请确认服务器可直连闲鱼）",
        400,
    )


def _fetch_item_detail(ctx: Context, item_id: str) -> dict:
    """只读抓单个商品详情（复用 capture 解析；只读，绝不发布/改闲鱼数据）。"""
    from .capture import extract_from_detail

    cookie = ctx.cookie_get()
    if not cookie:
        raise _RegError("尚未配置 Cookie，无法抓取该商品的发布素材", 400)

    detail_fn = ctx.fakes.get("detail_fn")
    if detail_fn is None:
        from .item_read_api import fetch_item_detail
        from .relist import clear_proxy_env

        def _real(cookie: str, item_id: str) -> dict:
            clear_proxy_env()
            return fetch_item_detail(cookie, item_id)

        detail_fn = _real
    try:
        payload = detail_fn(cookie, item_id)
    except Exception as exc:  # noqa: BLE001
        raise _RegError(
            f"抓取商品详情失败（可能是 Cookie 失效或商品未公开）: {type(exc).__name__}: {exc}",
            502,
        ) from exc
    if not isinstance(payload, dict):
        raise _RegError("商品详情返回格式异常", 502)
    info = extract_from_detail(payload)
    ret = info.get("ret") or []
    if isinstance(ret, (list, tuple)) and any("FAIL" in str(x) for x in ret):
        raise _RegError(f"商品详情拉取失败（可能 Cookie 失效或商品不可见）: {ret}", 502)
    return payload


# ---- app factory ----

def create_app(
    *,
    products_path: Optional[Path] = None,
    relist_db: Optional[Path] = None,
    control_path: Optional[Path] = None,
    deliveries_db: Optional[Path] = None,
    admin_token: Optional[str] = None,
    fakes: Optional[Dict[str, Callable]] = None,
    admin_token_persist: Optional[Callable[[str], None]] = None,
) -> Flask:
    from .config import config

    app = Flask(__name__)
    app.json.ensure_ascii = False

    ctx = Context(
        products_path=Path(products_path) if products_path else PROJECT_ROOT / "config" / "products.json",
        relist_db=Path(relist_db) if relist_db else PROJECT_ROOT / "data" / "relist.db",
        control_path=Path(control_path) if control_path else PROJECT_ROOT / "data" / "control.json",
        deliveries_db=Path(deliveries_db) if deliveries_db else PROJECT_ROOT / "data" / "deliveries.db",
        cookie_get=lambda: config.cookies_str,
        cookie_set=lambda c: config.set_cookie(c),
        myid=lambda: config.myid,
        admin_token=admin_token if admin_token is not None else os.getenv("ADMIN_TOKEN", "").strip(),
        fakes=fakes,
    )
    app.extensions["xy"] = ctx

    # ---- 鉴权 ----

    SESSION_COOKIE = "xy_admin_session"
    SESSION_TTL = 14 * 24 * 3600  # 登录会话有效期：14 天

    def _token_ok() -> bool:
        if not ctx.admin_token:
            return False
        # 兼容旧 header 方式（脚本/测试/命令行仍可用 X-Admin-Token）
        if request.headers.get("X-Admin-Token", "") == ctx.admin_token:
            return True
        # 新会话方式：HttpOnly cookie 指向服务端内存会话
        sid = request.cookies.get(SESSION_COOKIE, "") or ""
        expires = ctx.sessions.get(sid)
        if expires is None:
            return False
        if time.time() > expires:
            ctx.sessions.pop(sid, None)
            return False
        return True

    def _require_auth() -> Optional[Response]:
        if not _token_ok():
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.post("/api/login")
    def api_login():
        body = request.get_json(silent=True) or {}
        password = str(body.get("password") or "")
        if not ctx.admin_token:
            return jsonify({"error": "服务端未配置 ADMIN_TOKEN，无法登录"}), 403
        if password != ctx.admin_token:
            return jsonify({"error": "密码错误"}), 401
        sid = secrets.token_urlsafe(32)
        ctx.sessions[sid] = time.time() + SESSION_TTL
        resp = jsonify({"ok": True})
        resp.set_cookie(
            SESSION_COOKIE, sid, max_age=SESSION_TTL,
            httponly=True, samesite="Lax", path="/",
        )
        return resp

    @app.post("/api/logout")
    def api_logout():
        sid = request.cookies.get(SESSION_COOKIE, "") or ""
        ctx.sessions.pop(sid, None)
        resp = jsonify({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.post("/api/password")
    def api_password():
        """改登录密码：校验旧密码 → 更新内存+写 .env → 清空全部会话强制重登。"""
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        old_pw = str(body.get("old_password") or "")
        new_pw = str(body.get("new_password") or "").strip()
        if not ctx.admin_token:
            return jsonify({"error": "服务端未配置 ADMIN_TOKEN，无法改密码"}), 403
        if old_pw != ctx.admin_token:
            return jsonify({"error": "旧密码错误"}), 401
        if len(new_pw) < 8:
            return jsonify({"error": "新密码至少 8 位"}), 400
        if new_pw == ctx.admin_token:
            return jsonify({"error": "新密码不能与旧密码相同"}), 400
        ctx.admin_token = new_pw
        if admin_token_persist is not None:
            try:
                admin_token_persist(new_pw)
            except OSError:
                pass
        ctx.sessions.clear()
        resp = jsonify({"ok": True, "message": "密码已修改，请重新登录"})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ---- 页面 ----

    @app.get("/")
    def index():
        return Response(_PAGE_HTML, mimetype="text/html")  # werkzeug 自动补 ; charset=utf-8

    # ---- 商品 CRUD ----

    @app.get("/api/products")
    def api_products():
        if (err := _require_auth()) is not None:
            return err
        rows = []
        for key, entry in ctx.load_entries().items():
            if not isinstance(entry, dict):
                continue
            row = _entry_to_row(key, entry)
            row.update(_live_summary(ctx, row["key"]))
            rows.append(row)
        return jsonify(
            {
                "products": sorted(rows, key=lambda r: r["key"]),
                "products_version": ctx.products_version,
            }
        )

    @app.post("/api/products")
    def api_create_product():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        key = str(body.get("key") or "").strip()
        if not key:
            return jsonify({"error": "缺少 key"}), 400
        entries = ctx.load_entries()
        existing = entries.get(key)
        if existing is not None and str(existing.get("key") or key) == key:
            return jsonify({"error": f"商品 {key} 已存在，请用更新"}), 409
        entries[key] = {k: body[k] for k in _ALLOWED_FIELDS if k in body}
        entries[key]["key"] = key
        ctx.save_entries(entries)
        return jsonify({"ok": True, "key": key, "products_version": ctx.products_version})

    @app.put("/api/products/<key>")
    def api_update_product(key):
        if (err := _require_auth()) is not None:
            return err
        entries = ctx.load_entries()
        if key not in entries or not isinstance(entries[key], dict):
            return jsonify({"error": f"商品 {key} 不存在"}), 404
        body = request.get_json(silent=True) or {}
        for field in _ALLOWED_FIELDS - {"key"}:
            if field in body:
                entries[key][field] = body[field]
        ctx.save_entries(entries)
        return jsonify({"ok": True, "key": key, "products_version": ctx.products_version})

    @app.delete("/api/products/<key>")
    def api_delete_product(key):
        if (err := _require_auth()) is not None:
            return err
        entries = ctx.load_entries()
        if key not in entries:
            return jsonify({"error": f"商品 {key} 不存在"}), 404
        del entries[key]
        ctx.save_entries(entries)
        return jsonify({"ok": True, "products_version": ctx.products_version})

    # ---- 登记已发布的商品（app 分享链接 → 自动发货商品）----

    @app.post("/api/items/resolve")
    def api_items_resolve():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        try:
            item_id = _resolve_id(ctx, str(body.get("text") or ""))
        except _RegError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return jsonify(
            {
                "ok": True,
                "item_id": item_id,
                "known": bool(ctx.find_product_by_item(item_id)),
                "product_key": ctx.find_product_by_item(item_id),
            }
        )

    @app.post("/api/items/preview")
    def api_items_preview():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        try:
            item_id = _resolve_id(ctx, str(body.get("text") or ""))
            payload = _fetch_item_detail(ctx, item_id)
        except _RegError as exc:
            return jsonify({"error": str(exc)}), exc.status
        from .capture import extract_from_detail

        info = extract_from_detail(payload)
        return jsonify(
            {
                "ok": True,
                "item_id": item_id,
                "known": bool(ctx.find_product_by_item(item_id)),
                "product_key": ctx.find_product_by_item(item_id),
                "title": info.get("title") or "",
                "description": (info.get("description") or "")[:120],
                "images": info.get("images") or [],
                "image_count": info.get("image_count") or 0,
                "price_candidates": (info.get("price_candidates") or [])[:5],
            }
        )

    @app.post("/api/items/register")
    def api_items_register():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        auto_relist = bool(body.get("auto_relist", True))
        name = str(body.get("name") or "").strip()
        message = str(body.get("message") or "").strip()
        price = str(body.get("price") or "").strip()
        delivery = str(body.get("delivery") or "无需邮寄").strip()
        if not message:
            return jsonify({"error": "请填话术 message（自动发货要发给买家的内容）"}), 400
        try:
            item_id = _resolve_id(ctx, str(body.get("text") or ""))
        except _RegError as exc:
            return jsonify({"error": str(exc)}), exc.status

        existing = ctx.find_product_by_item(item_id)
        if existing is not None:
            return (
                jsonify({"error": f"该 item_id 已属于商品 {existing}（可在表格里直接编辑话术/停用）"}),
                409,
            )

        listing = None
        if auto_relist:
            try:
                payload = _fetch_item_detail(ctx, item_id)
                from .capture import build_listing_from_detail

                listing = build_listing_from_detail(
                    payload,
                    item_id=item_id,
                    name=name,
                    price=price,
                    delivery=delivery,
                )
            except _RegError as exc:
                return jsonify({"error": str(exc)}), exc.status
            except ValueError as exc:
                return jsonify({"error": f"{exc}（可取消“自动铺货”只做自动发货）"}), 400
            name = name or listing["title"]
        else:
            name = name or f"商品 {item_id}"

        entries = ctx.load_entries()
        entries[item_id] = {
            "key": item_id,
            "name": name,
            "message": message,
            "enabled": True,
            "source_item_id": item_id,
            "relist": auto_relist,
        }
        if listing is not None:
            entries[item_id]["listing"] = listing
        ctx.save_entries(entries)
        return jsonify(
            {
                "ok": True,
                "key": item_id,
                "name": name,
                "relist": auto_relist,
                "products_version": ctx.products_version,
            }
        )

    # ---- Cookie ----

    @app.post("/api/cookie")
    def api_cookie():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        cookie = str(body.get("cookie") or "").strip()
        if not cookie or cookie == "your_cookies_here":
            return jsonify({"error": "cookie 为空或占位值"}), 400
        ctx.cookie_set(cookie)
        ctx.write_control(cookie=cookie)
        return jsonify({"ok": True, "updated": True})

    @app.post("/api/cookie/test")
    def api_cookie_test():
        """探测当前 Cookie 是否生效（复用 cookiewatch/token_api 判定，不告警、不写文件）。"""
        if (err := _require_auth()) is not None:
            return err
        cookie = ctx.cookie_get()
        if not cookie or cookie == "your_cookies_here":
            return jsonify({"ok": False, "status": "no_cookie",
                            "message": "尚未配置 Cookie，请先粘贴并更新"}), 400

        import requests
        from .token_api import TokenFetchError

        check = ctx.fakes.get("cookie_check")
        if check is None:
            from .token_api import fetch_access_token

            def _real_check(c: str, m: str) -> str:
                return fetch_access_token(c, m)

            check = _real_check
        try:
            check(cookie, ctx.myid())
        except TokenFetchError as exc:
            text = str(exc)
            if any(k in text for k in ("RGV587", "风控", "被挤爆")):
                return jsonify({"ok": False, "status": "risk",
                                "message": "Cookie 触发平台风控（RGV587），已暂停相关操作：请稍后再试或换网/换号"})
            return jsonify({"ok": False, "status": "invalid",
                            "message": "Cookie 已失效，无法换取 token：请在平台上重新登录后复制新 Cookie"})
        except requests.RequestException:
            return jsonify({"ok": False, "status": "network",
                            "message": "网络不可达，无法判定（非失效），请稍后重试"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "status": "error",
                            "message": f"探测异常: {type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "status": "ok", "message": "Cookie 有效，能正常换取 token"})

    # ---- 触发动作 ----

    @app.post("/api/backfill")
    def api_backfill():
        if (err := _require_auth()) is not None:
            return err
        catalog = ctx.catalog()
        keys = catalog.active_product_keys()
        if not keys:
            return jsonify({"error": "没有 enabled 商品"}), 400

        async def _run_all():
            mgr = ctx.manager()
            out = {}
            for k in keys:
                out[k] = await mgr.ensure_min_online(k, trigger="admin_backfill")
            return out

        try:
            results = asyncio.run(_run_all())
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"backfill 失败: {type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True, "results": results})

    @app.post("/api/autoship")
    def api_autoship():
        if (err := _require_auth()) is not None:
            return err
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("item_id") or body.get("product_key") or "").strip()
        buyer_id = str(body.get("buyer_id") or "").strip()
        order_id = str(body.get("order_id") or "").strip()
        if not (item_id and buyer_id and order_id):
            return jsonify({"error": "需要 item_id/product_key、buyer_id、order_id"}), 400
        if not ctx.cookie_get():
            return jsonify({"error": "尚未配置 Cookie"}), 400

        product = ctx.catalog().resolve(item_id)
        if product is None:
            return jsonify({"error": f"商品未命中或已停用: {item_id}"}), 404
        message = str(product.get("message") or "")
        if not message:
            return jsonify({"error": "该商品未配置话术 message"}), 400

        send_fn = ctx.fakes.get("send_fn")
        if send_fn is None:
            from .ws_sender import RealMessageSender

            async def _real_send():
                from .config import config

                sender = RealMessageSender(config.cookies_str, own_id=config.myid)
                return bool(await sender.send_to_buyer(buyer_id, message))

            send_fn = _real_send
        try:
            sent = asyncio.run(send_fn()) if asyncio.iscoroutinefunction(send_fn) else send_fn(buyer_id, message)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"话术发送失败（不标记发货）: {type(exc).__name__}: {exc}"}), 500
        if not sent:
            return jsonify({"error": "话术未获服务器 ACK，结果未知，不标记发货"}), 500

        ship_fn = ctx.fakes.get("ship_fn")
        if ship_fn is None:
            from .ship_api import ShipConfirmError, confirm_dummy_ship

            def _real_ship(cookies: str, order: str) -> bool:
                try:
                    confirm_dummy_ship(cookies, order)
                    return True
                except ShipConfirmError as exc:
                    raise RuntimeError(str(exc)) from exc

            ship_fn = _real_ship
        try:
            ok = asyncio.run(ship_fn(ctx.cookie_get(), order_id)) if asyncio.iscoroutinefunction(ship_fn) else ship_fn(ctx.cookie_get(), order_id)
            if not ok:
                return jsonify({"error": "发货确认返回失败"}), 500
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"发货确认失败: {type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True, "order_id": order_id, "item_id": item_id})

    # ---- 报表 ----

    @app.get("/api/report")
    def api_report():
        if (err := _require_auth()) is not None:
            return err
        try:
            payload = ctx.manager().report()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"报表生成失败: {type(exc).__name__}: {exc}"}), 500
        for row in payload["products"]:
            live = _live_summary(ctx, row["product_key"])
            row.update(live)
        return jsonify(payload)

    return app


def _live_summary(ctx: Context, product_key: str) -> Dict[str, Any]:
    """只读实时附加信息：在售份数（网络，失败为 None）/ 已售 / 别名 / 最近补发。"""
    catalog = ctx.catalog()
    summary: Dict[str, Any] = {"on_sale_now": None, "total_sales": 0}

    try:
        counts = catalog.counts_by_product(ctx.deliveries_db)
        summary["total_sales"] = int(counts.get(product_key) or 0)
    except Exception:  # noqa: BLE001
        pass

    catalog_key = None
    record = catalog.product(product_key)
    catalog_key = product_key if record is not None else None
    if catalog_key is not None:
        summary["aliases"] = catalog.alias_ids(product_key)
        logs = catalog.publish_logs(product_key=product_key, limit=3)
        summary["latest_publishes"] = [
            {
                "item_id": log["item_id"],
                "trigger": log["trigger"],
                "ok": log["ok"],
                "note": log["note"],
                "at": log["created_at"],
            }
            for log in logs
        ]
        try:
            summary["on_sale_now"] = asyncio.run(_count_one(ctx, product_key))
        except Exception:  # noqa: BLE001  在售拉取失败不影响页面
            summary["on_sale_now"] = None
    return summary


async def _count_one(ctx: Context, product_key: str) -> int:
    return await ctx.manager().count_on_sale(product_key)


# ---- 单页前端（无第三方 CDN） ----
# 注意：raw 字符串，必须保留 JS 内 \' 转义（Python 普通三引号会吞掉反斜杠，
# 导致下发的前端 script 引号错乱、整页空白）。

_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>闲鱼自动发货 · 管理页</title>
<style>
  body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:24px;background:#f7f8fa;color:#222}
  h1{font-size:20px}.muted{color:#888;font-size:13px}
  .card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px 18px;margin-bottom:18px}
  button{cursor:pointer;border:1px solid #cdd3da;background:#fff;border-radius:6px;padding:6px 12px}
  button.primary{background:#ff5000;border-color:#ff5000;color:#fff}
  button.danger{color:#c0392b;border-color:#e8b4b0}
  button.small{padding:2px 8px;font-size:12px;margin-left:6px}
  textarea,input[type=text]{width:100%;box-sizing:border-box;border:1px solid #cdd3da;border-radius:6px;padding:6px 8px;font-family:ui-monospace,Consolas,monospace}
  textarea{min-height:60px;white-space:pre}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #eef0f2;vertical-align:top}
  th{color:#666;font-weight:600}
  .badge{display:inline-block;border-radius:10px;padding:1px 8px;font-size:12px}
  .on{background:#e6f7e6;color:#2e7d32}.off{background:#fdecea;color:#c0392b}
  #gate{max-width:360px;margin:120px auto}.row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
  .grow{flex:1}code{background:#f2f3f5;padding:1px 5px;border-radius:4px;font-size:12px}
  label.fld{width:88px;color:#666;font-size:13px;flex:none}
  .row textarea{flex:1;width:auto}
</style>
</head>
<body>
<div id="gate" class="card" style="display:none">
  <h1>管理页登录</h1>
  <p class="muted">输入登录密码（即 .env 里的 ADMIN_TOKEN）。密码错误无法进入；登录后 14 天内自动保持，无需每次输入。</p>
  <div class="row"><input id="token" type="password" placeholder="登录密码" class="grow">
  <button class="primary" onclick="login()">进入</button></div>
  <div id="gateMsg" class="muted"></div>
</div>

<div id="app" style="display:none">
  <div class="card">
    <div class="row"><h1 style="margin:0">闲鱼自动发货 · 管理页</h1>
      <button onclick="refresh()">刷新</button>
      <button onclick="showPwd()">修改密码</button>
      <button onclick="logout()">退出登录</button></div>
    <div class="muted">商品改动即时生效到运行中的 bot；「更新 Cookie」用于 Cookie 失效后热恢复。</div>
  </div>

  <div id="pwdPanel" class="card" style="display:none">
    <h3>修改登录密码</h3>
    <p class="muted">密码即服务端 .env 的 ADMIN_TOKEN，修改后立即生效并持久化；所有已登录会话将强制重新登录。</p>
    <div class="row"><label class="fld">旧密码</label><input id="pwdOld" type="password" class="grow"></div>
    <div class="row"><label class="fld">新密码</label><input id="pwdNew" type="password" placeholder="至少 8 位" class="grow"></div>
    <div class="row"><button class="primary" onclick="changePwd()">保存新密码</button><span id="pwdMsg" class="muted"></span></div>
  </div>

  <div class="card">
    <h3>商品 / 话术 / 发布模板
      <button class="primary small" onclick="newProduct()">新建</button>
      <button class="small" onclick="toggleReg()">从链接登记（自动发货）</button>
    </h3>
    <div id="productError" class="muted" style="color:#c0392b"></div>
    <table>
      <thead><tr><th>key</th><th>名称</th><th>状态</th><th>话术</th><th>在售/已售</th><th>补发记录</th><th>操作</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <div id="regPanel" class="card" style="display:none">
    <div class="row"><h3 style="margin:0">登记已发布商品 → 自动发货</h3>
      <button onclick="toggleReg()">收起</button></div>
    <p class="muted">闲鱼 app 发布商品后复制「分享链接」粘贴进来 → 点“解析”。填话术（发给买家的内容）后登记：bot 监听到该商品成交即自动发话术并标发货。勾选“自动铺货”会从该商品抓发布素材作模板，成交后自动补新链接保持多份在线。</p>
    <div class="row"><input id="regLink" type="text" placeholder="粘贴闲鱼分享链接，或直接填 10~15 位商品 id" class="grow">
      <button class="primary" onclick="regParse()">解析</button></div>
    <div id="regPrev" class="muted" style="white-space:pre-wrap;min-height:0"></div>
    <div class="row"><label class="fld">名称</label>
      <input id="regName" type="text" placeholder="留空则用抓到的标题" class="grow"></div>
    <div class="row"><label class="fld">话术</label>
      <textarea id="regMsg" placeholder="自动发货发给买家的内容（夸克分享文案等）"></textarea></div>
    <div class="row"><label class="fld">定价(元)</label>
      <input id="regPrice" type="text" placeholder="自动铺货的发布价" style="width:120px">
      <label><input id="regAuto" type="checkbox" checked> 成交后自动铺货补份</label></div>
    <div class="row"><label class="fld">物流</label>
      <input id="regDelivery" type="text" value="无需邮寄" style="width:120px">
      <button class="primary" onclick="regSubmit()">登记并启动自动发货</button>
      <span id="regFeedback" class="muted"></span></div>
    <div id="regErr" class="muted" style="color:#c0392b"></div>
  </div>

  <div class="card">
    <h3>运行凭证</h3>
    <div class="muted">更新后 bot 约 5s 内热刷新并按新 Cookie 重连。粘贴完整 Cookie（含 unb=…）。</div>
    <textarea id="cookieBox" placeholder="unb=...; cookie2=...; ..."></textarea>
    <div class="row"><button onclick="testCookie()">测试当前 Cookie</button>
      <button class="primary" onclick="updateCookie()">更新 Cookie</button>
      <span id="cookieMsg" class="muted"></span></div>
  </div>

  <div class="card">
    <h3>手动动作（写操作）</h3>
    <div class="row">
      <button class="primary" onclick="backfill()">补货到在线 N 份</button>
      <span class="muted">按各商品在售数补足（真实发布）</span>
    </div>
    <div class="muted" style="margin:6px 0">手动发货：填真实 item_id / 买家 id / 订单号，发话术并标记发货（用于 bot 漏发时补发）。</div>
    <div class="row"><input id="shipItem" type="text" placeholder="item_id" class="grow">
      <input id="shipBuyer" type="text" placeholder="buyer_id" class="grow">
      <input id="shipOrder" type="text" placeholder="order_id" class="grow"></div>
    <div class="row"><button onclick="autoship()">手动发货</button><span id="actionMsg" class="muted"></span></div>
  </div>

  <div class="card">
    <button onclick="report()">查看报表</button>
    <div id="reportBox" class="muted" style="white-space:pre-wrap;margin-top:8px"></div>
  </div>

  <div id="editor" class="card" style="display:none">
    <h3 id="editorTitle">编辑商品</h3>
    <p class="muted">JSON 字段：name / message / enabled / source_item_id / aliases[] / listing{title,description,images[],price,delivery}。修改后即生效。</p>
    <textarea id="editorJson"></textarea>
    <div class="row"><button class="primary" onclick="saveEditor()">保存</button>
      <button onclick="closeEditor()">取消</button><span id="editorError" class="muted" style="color:#c0392b"></span></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
function api(method, url, body) {
  // 鉴权走 HttpOnly 会话 cookie，随请求自动带上；不再存 token 到 localStorage
  const opt = {method, headers: {}};
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  return fetch(url, opt).then(async res => {
    let data = null; try { data = await res.json(); } catch (e) {}
    if (res.status === 401) { showGate(); throw new Error((data && data.error) || 'unauthorized'); }
    if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
    return data;
  });
}
function showGate() { $('app').style.display = 'none'; $('gate').style.display = 'block'; }
function enterApp() { $('gate').style.display = 'none'; $('app').style.display = 'block'; }
async function boot() {
  try { await api('GET', '/api/products'); enterApp(); await refresh(); }
  catch (e) { showGate(); }
}
async function login() {
  const pw = $('token').value;
  if (!pw) return;
  $('gateMsg').textContent = '';
  try {
    await api('POST', '/api/login', {password: pw});
    $('token').value = '';
    enterApp();
    await refresh();
  } catch (e) {
    if (e.message === '密码错误') { $('gateMsg').textContent = '密码错误，请重试'; }
    else if (e.message === 'unauthorized') { $('gateMsg').textContent = '密码错误，请重试'; }
    else { $('gateMsg').textContent = '登录失败：' + e.message; }
  }
}
async function logout() {
  try { await api('POST', '/api/logout'); } catch (e) {}
  showGate();
}
function showPwd() {
  const el = $('pwdPanel');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
async function changePwd() {
  const oldPw = $('pwdOld').value, newPw = $('pwdNew').value;
  $('pwdMsg').textContent = '';
  if (!oldPw || !newPw) { $('pwdMsg').textContent = '请填旧密码与新密码'; return; }
  try {
    const r = await api('POST', '/api/password', {old_password: oldPw, new_password: newPw});
    $('pwdMsg').textContent = r.message || '密码已修改';
    $('pwdOld').value = ''; $('pwdNew').value = '';
    $('pwdPanel').style.display = 'none';
    showGate();
    $('gateMsg').textContent = '密码已修改，请用新密码重新登录';
  } catch (e) { $('pwdMsg').textContent = e.message; }
}
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
function liveText(r) {
  const parts = [];
  parts.push('在售 ' + (r.on_sale_now === null || r.on_sale_now === undefined ? '—' : r.on_sale_now));
  parts.push('已售 ' + r.total_sales);
  return parts.join(' · ');
}
async function refresh() {
  const data = await api('GET', '/api/products');
  const tb = $('rows'); tb.innerHTML = '';
  for (const r of data.products) {
    const tr = document.createElement('tr');
    const pub = (r.latest_publishes || []).slice(0,2).map(p =>
      (p.ok ? '✓' : '✗') + ' ' + (p.item_id ? p.item_id : '—') + (p.trigger === 'after_sale' ? ' 成交补' : p.trigger === 'backfill' || p.trigger === 'admin_backfill' ? ' 铺货' : '') ).join('<br>') || '—';
    tr.innerHTML = '<td><code>' + esc(r.key) + '</code></td>' +
      '<td>' + esc(r.name) + '</td>' +
      '<td><span class="badge ' + (r.enabled ? 'on' : 'off') + '">' + (r.enabled ? '在线' : '停用') + '</span></td>' +
      '<td>' + esc((r.message || '').slice(0, 18)) + '</td>' +
      '<td>' + liveText(r) + '</td>' +
      '<td>' + pub + '</td>' +
      '<td><button class="small" onclick="toggle(\'' + esc(r.key) + '\',' + (r.enabled ? 'false' : 'true') + ')">' + (r.enabled ? '停用' : '启用') + '</button>' +
      '<button class="small" onclick="edit(\'' + esc(r.key) + '\')">编辑</button>' +
      '<button class="small danger" onclick="del(\'' + esc(r.key) + '\')">删除</button></td>';
    tb.appendChild(tr);
  }
  $('productError').textContent = '';
}
async function toggle(key, enabled) {
  try { await api('PUT', '/api/products/' + encodeURIComponent(key), {enabled}); refresh(); }
  catch (e) { $('productError').textContent = e.message; }
}
let editingKey = null;
async function edit(key) {
  editingKey = key;
  const data = await api('GET', '/api/products');
  const r = data.products.find(x => x.key === key);
  if (!r) return;
  $('editorTitle').textContent = '编辑商品 ' + key;
  const clean = {key: r.key, name: r.name, message: r.message, enabled: r.enabled};
  if (r.source_item_id) clean.source_item_id = r.source_item_id;
  if (r.listing) clean.listing = r.listing;
  $('editorJson').value = JSON.stringify(clean, null, 2);
  $('editor').style.display = 'block'; $('editorError').textContent = '';
}
function newProduct() {
  editingKey = null;
  $('editorTitle').textContent = '新建商品';
  $('editorJson').value = JSON.stringify({key: '', name: '新商品', message: '', enabled: true}, null, 2);
  $('editor').style.display = 'block'; $('editorError').textContent = '';
}
function closeEditor() { $('editor').style.display = 'none'; }
async function saveEditor() {
  try {
    const body = JSON.parse($('editorJson').value);
    if (!body.key) { $('editorError').textContent = '缺少 key'; return; }
    if (editingKey) await api('PUT', '/api/products/' + encodeURIComponent(editingKey), body);
    else await api('POST', '/api/products', body);
    closeEditor(); refresh();
  } catch (e) { $('editorError').textContent = e.message; }
}
async function del(key) {
  if (!confirm('删除商品 ' + key + '？历史别名/成交记录仍保留在数据库。')) return;
  try { await api('DELETE', '/api/products/' + encodeURIComponent(key)); refresh(); }
  catch (e) { $('productError').textContent = e.message; }
}
function setMsg(id, text, ok) {
  const el = $(id); el.textContent = text;
  el.style.color = ok === undefined ? '' : (ok ? '#2e7d32' : '#c0392b');
}
async function testCookie() {
  setMsg('cookieMsg', '正在探测当前 Cookie …');
  try {
    const r = await api('POST', '/api/cookie/test', {});
    setMsg('cookieMsg', r.message, r.ok);
  } catch (e) { setMsg('cookieMsg', e.message, false); }
}
async function updateCookie() {
  const cookie = $('cookieBox').value.trim();
  try {
    const r = await api('POST', '/api/cookie', {cookie});
    setMsg('cookieMsg', r.ok ? '已更新，bot 将自动热刷新' : '', true);
  } catch (e) { setMsg('cookieMsg', e.message, false); }
}
async function backfill() {
  if (!confirm('将对所有启用商品各补足在线份数（真实发布）。确认？')) return;
  try {
    const r = await api('POST', '/api/backfill', {});
    $('actionMsg').textContent = '已执行：' + JSON.stringify(r.results);
  } catch (e) { $('actionMsg').textContent = e.message; }
}
async function autoship() {
  const payload = {item_id: $('shipItem').value.trim(), buyer_id: $('shipBuyer').value.trim(), order_id: $('shipOrder').value.trim()};
  try {
    const r = await api('POST', '/api/autoship', payload);
    $('actionMsg').textContent = r.ok ? ('已发货 order=' + r.order_id) : '';
  } catch (e) { $('actionMsg').textContent = e.message; }
}
async function report() {
  try {
    const data = await api('GET', '/api/report');
    $('reportBox').textContent = JSON.stringify(data, null, 2);
  } catch (e) { $('reportBox').textContent = '报表失败：' + e.message; }
}
let regItemId = null;
function toggleReg() {
  const el = $('regPanel');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
async function regParse() {
  const text = $('regLink').value.trim();
  $('regErr').textContent = ''; $('regFeedback').textContent = '';
  if (!text) { $('regErr').textContent = '请先粘贴分享链接或填商品 id'; return; }
  let id;
  try {
    const known = await api('POST', '/api/items/resolve', {text});
    id = known.item_id;
    if (known.known) {
      $('regPrev').textContent = '已登记为「' + known.product_key + '」，请在上方表格直接编辑它，无需重复登记。';
      return;
    }
  } catch (e) { $('regErr').textContent = e.message; return; }
  // 解析成功即可登记「仅自动发货」。抓详情素材只是为自动铺货做准备，
  // 单独做、可失败：被闲鱼风控拦截（RGV587）时不阻塞登记。
  regItemId = id;
  $('regPrev').textContent = '解析成功：item_id=' + id + '，正在抓取商品素材作自动铺货模板…';
  try {
    const r = await api('POST', '/api/items/preview', {text});
    if (r.title) $('regName').value = r.title;
    $('regPrice').value = '';
    const cand = (r.price_candidates && r.price_candidates.length)
      ? ' 详情参考价：' + JSON.stringify(r.price_candidates.slice(0, 3)) : '';
    $('regPrev').textContent = 'item_id=' + id + (r.title ? '\n标题：' + r.title : '') +
      '\n图片 ' + (r.image_count || 0) + ' 张' + cand +
      '\n已勾选自动铺货。确认名称/话术/定价后点「登记并启动自动发货」。';
  } catch (e) {
    $('regAuto').checked = false;
    $('regPrev').textContent = '解析成功：item_id=' + id +
      '。抓详情素材被闲鱼风控拦截（RGV587），已取消勾选「自动铺货」。\n' +
      '仍可填话术直接登记「仅自动发货」先跑起来；风控过去后再回来勾选自动铺货补模板。';
  }
}
async function regSubmit() {
  const payload = {
    text: $('regLink').value.trim(),
    name: $('regName').value.trim(),
    message: $('regMsg').value.trim(),
    price: $('regPrice').value.trim(),
    delivery: $('regDelivery').value.trim(),
    auto_relist: $('regAuto').checked,
  };
  $('regErr').textContent = '';
  if (!payload.message) { $('regErr').textContent = '请填话术（自动发货内容）'; return; }
  try {
    const r = await api('POST', '/api/items/register', payload);
    $('regFeedback').textContent = '已登记「' + r.name + '」（' + r.key + '，' +
      (r.relist ? '自动铺货' : '仅自动发货') + '），约 5s 后 bot 热加载生效。';
    regItemId = null;
    $('regMsg').value = ''; $('regName').value = ''; $('regLink').value = '';
    $('regPrev').textContent = '';
    refresh();
  } catch (e) { $('regErr').textContent = e.message; }
}
boot();
</script>
</body>
</html>
"""


def _persist_admin_token(new_token: str) -> None:
    """把新密码写回 .env 的 ADMIN_TOKEN（改密码需持久化，重启仍生效）。"""
    from dotenv import set_key

    from .config import ENV_PATH

    set_key(ENV_PATH, "ADMIN_TOKEN", new_token)


def main() -> None:
    host = os.getenv("CONTROL_HOST", DEFAULT_HOST)
    try:
        port = int(os.getenv("CONTROL_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    app = create_app(admin_token_persist=_persist_admin_token)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
