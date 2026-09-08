"""SQLite 防重复存储。

只维护一张表 deliveries，delivery_key 为 UNIQUE 主键，用于幂等防重复。
Phase 2 不建立完整订单表。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import PROJECT_ROOT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_key TEXT PRIMARY KEY,
    order_id TEXT,
    buyer_id TEXT,
    item_id TEXT,
    status TEXT,
    created_at TEXT,
    delivered_at TEXT
)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeliveryStore:
    def __init__(self, db_path=None) -> None:
        if db_path is None:
            db_path = PROJECT_ROOT / "data" / "deliveries.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def already_delivered(self, delivery_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_key = ?", (delivery_key,)
        ).fetchone()
        return row is not None

    def record_delivery(
        self,
        *,
        delivery_key: str,
        order_id: Optional[str],
        buyer_id: Optional[str],
        item_id: Optional[str],
    ) -> bool:
        """插入一条已发货记录。delivery_key 冲突（已存在）返回 False。"""
        now = _utc_now()
        try:
            self.conn.execute(
                "INSERT INTO deliveries "
                "(delivery_key, order_id, buyer_id, item_id, status, created_at, delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (delivery_key, order_id, buyer_id, item_id, "delivered", now, now),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def close(self) -> None:
        self.conn.close()
