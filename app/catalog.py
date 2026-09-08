"""商品目录 + item_id 别名注册表（补货核心）。

闲鱼每次发布都会生成全新的 item_id（quantity 恒为 1），因此同款商品的
N 份在线链接是不同的 item_id。本模块把 item_id 归一到"稳定商品"：

- config/products.json 每个条目即一个商品，条目键作为 product_key；
- data/relist.db 的 item_aliases 表登记「某 item_id 属于哪个 product_key」
  （含每次补发的新链接 + 配置里的历史 id），使自动发货对任意一份同款
  都能命中同一商品的话术；
- 同库 publish_log 记录每一次真实补发，全部留痕。

兼容旧扁平格式（item_id -> {name, message}）与新格式（可含 listing 发布
模板）。item_map() 返回实时共享映射：register_alias 会即时更新它，供
AutoShipTrigger/order_context 无需改动即可识别新补的链接。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT

PRODUCTS_PATH = PROJECT_ROOT / "config" / "products.json"
RELIST_DB_PATH = PROJECT_ROOT / "data" / "relist.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_aliases (
    item_id TEXT PRIMARY KEY,
    product_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key TEXT NOT NULL,
    item_id TEXT,
    trigger TEXT,
    ref_order TEXT,
    ok INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class Catalog:
    """加载商品配置与别名，提供 item_id -> product 的实时解析。"""

    def __init__(self, products_path=None, db_path=None, min_online: int = 2) -> None:
        self.products_path = Path(products_path) if products_path else PRODUCTS_PATH
        db_path = Path(db_path) if db_path else RELIST_DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.min_online = min_online
        self.records: Dict[str, Dict[str, Any]] = {}
        self._alias_to_key: Dict[str, str] = {}
        self._by_item: Dict[str, Dict[str, Any]] = {}

        self._apply_products_data(_read_json(self.products_path))

        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._seed_db_aliases()
        self._load_db_aliases()
        self._rebuild_item_map()

    # ---- 数据加载 ----

    def _apply_products_data(self, data: Dict[str, Any]) -> None:
        """解析 products.json（新旧格式）到 self.records（含 enabled）。"""
        self.records.clear()
        products_src: Dict[str, Any] = data or {}
        if isinstance(data.get("products"), dict):
            products_src = data["products"]
            raw_min = data.get("min_online")
            if isinstance(raw_min, int) and raw_min > 0:
                self.min_online = raw_min

        for key, value in products_src.items():
            if not isinstance(value, dict):
                continue
            product_key = str(value.get("key") or key)
            raw_source = value.get("source_item_id")
            source_ids = [str(raw_source)] if raw_source and str(raw_source).strip() else []
            aliases = value.get("aliases")
            if isinstance(aliases, list):
                source_ids += [str(a) for a in aliases if str(a).strip()]
            if not source_ids:
                source_ids = [str(key)]  # 旧扁平格式：条目键即 item_id
            record = {
                "key": product_key,
                "name": str(value.get("name") or ""),
                "message": str(value.get("message") or ""),
                "listing": value.get("listing") if isinstance(value.get("listing"), dict) else None,
                "source_ids": source_ids,
                "enabled": bool(value.get("enabled", True)),
                # relist=false 的商品只自动发货，不参与自动铺货（成交后不补份）
                "relist": bool(value.get("relist", True)),
            }
            self.records[product_key] = record
            for source_id in source_ids:
                if source_id.strip():
                    self._alias_to_key[source_id.strip()] = product_key

    def reload(self) -> None:
        """重读 config/products.json（in-place）。

        trigger/manager 持有的是同一批 record 引用，重载即可让新商品/停用即时生效，
        无需重建对象。保留 sqlite 连接与已登记别名（新 source_id 幂等播种）。
        """
        self._apply_products_data(_read_json(self.products_path))
        self._seed_db_aliases()
        self._load_db_aliases()
        self._rebuild_item_map()

    # ---- 别名持久化 ----

    def _seed_db_aliases(self) -> None:
        """把配置里的历史 item_id 播种进别名表（幂等）。"""
        now = _utc_now()
        for product_key, record in self.records.items():
            for source_id in record["source_ids"]:
                self._conn.execute(
                    "INSERT OR IGNORE INTO item_aliases (item_id, product_key, created_at) "
                    "VALUES (?, ?, ?)",
                    (source_id, product_key, now),
                )
        self._conn.commit()

    def _load_db_aliases(self) -> None:
        rows = self._conn.execute(
            "SELECT item_id, product_key FROM item_aliases"
        ).fetchall()
        for item_id, product_key in rows:
            self._alias_to_key[str(item_id)] = str(product_key)

    def _rebuild_item_map(self) -> None:
        """重建 active item_map（仅 enabled 商品，供自动发货/自动补货命中）。"""
        self._by_item.clear()
        for item_id, product_key in self._alias_to_key.items():
            record = self.records.get(product_key)
            if record is not None and record.get("enabled", True):
                self._by_item[item_id] = record

    def register_alias(self, item_id: str, product_key: str) -> bool:
        """登记 item_id 属于 product_key（补发成功后被调用）。即时对进程可见。"""
        item_id = str(item_id).strip()
        if not item_id or product_key not in self.records:
            return False
        self._conn.execute(
            "INSERT OR IGNORE INTO item_aliases (item_id, product_key, created_at) "
            "VALUES (?, ?, ?)",
            (item_id, product_key, _utc_now()),
        )
        self._conn.commit()
        if product_key != self._alias_to_key.get(item_id):
            self._alias_to_key[item_id] = product_key
            record = self.records.get(product_key)
            if record is not None:
                self._by_item[item_id] = record
        return True

    def log_publish(
        self,
        product_key: str,
        *,
        item_id: str = "",
        trigger: str = "",
        ref_order: str = "",
        ok: bool,
        note: str = "",
    ) -> None:
        cur = self._conn.execute(
            "INSERT INTO publish_log "
            "(product_key, item_id, trigger, ref_order, ok, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_key, item_id, trigger, ref_order, 1 if ok else 0, note, _utc_now()),
        )
        self._conn.commit()

    def publish_logs(self, product_key: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if product_key:
            rows = self._conn.execute(
                "SELECT id, product_key, item_id, trigger, ref_order, ok, note, created_at "
                "FROM publish_log WHERE product_key = ? ORDER BY id DESC LIMIT ?",
                (product_key, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, product_key, item_id, trigger, ref_order, ok, note, created_at "
                "FROM publish_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0], "product_key": r[1], "item_id": r[2], "trigger": r[3],
                "ref_order": r[4], "ok": bool(r[5]), "note": r[6], "created_at": r[7],
            }
            for r in rows
        ]

    def alias_ids(self, product_key: str) -> List[str]:
        return [
            item_id
            for item_id, key in self._alias_to_key.items()
            if key == product_key
        ]

    # ---- 查询 ----

    def product(self, product_key: str) -> Optional[Dict[str, Any]]:
        return self.records.get(product_key)

    def product_keys(self) -> List[str]:
        return list(self.records.keys())

    def active_product_keys(self) -> List[str]:
        """enabled 的商品 key（自动补货/报表只对它们动作）。"""
        return [key for key, record in self.records.items() if record.get("enabled", True)]

    def resolve(self, item_id: str) -> Optional[Dict[str, Any]]:
        """item_id -> 商品记录（仅 enabled；停用商品的链接按未命中处理）。"""
        if item_id is None:
            return None
        record = self._by_item.get(str(item_id))
        return record if record is not None else None

    def _lookup(self, item_id: str) -> Optional[Dict[str, Any]]:
        """含停用商品的别名解析（历史成交统计用，与自动发货命中无关）。"""
        if item_id is None:
            return None
        key = self._alias_to_key.get(str(item_id))
        return self.records.get(key) if key else None

    def listing_template(self, product_key: str) -> Optional[Dict[str, Any]]:
        record = self.records.get(product_key)
        return record["listing"] if record else None

    def item_map(self) -> Dict[str, Dict[str, Any]]:
        """实时共享的 item_id -> product 映射（含 message/name/product_key）。"""
        return self._by_item

    # ---- 其它 ----

    def counts_by_product(self, deliveries_db_path) -> Dict[str, int]:
        """按商品统计已发货单数（读 deliveries.db，不做任何写）。"""
        path = Path(deliveries_db_path)
        if not path.exists():
            return {key: 0 for key in self.records}
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT item_id FROM deliveries").fetchall()
        finally:
            conn.close()
        result = {key: 0 for key in self.records}
        for (item_id,) in rows:
            record = self._lookup(item_id)
            key = record["key"] if record else None
            if key in result:
                result[key] += 1
        return result

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
