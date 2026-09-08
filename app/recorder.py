"""事件记录器：将每个解码事件落盘到 logs/raw_events/。

- 每个事件一个 JSON 文件（含原始帧 + 解密消息 + 解析摘要）
- 追加一个 events.jsonl 汇总索引，便于快速浏览
- 全部为本地写操作，仅记录、不发送
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import config


class EventRecorder:
    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or config.log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "events.jsonl"
        self._seq = self._count_existing()

    def _count_existing(self) -> int:
        if not self.jsonl_path.exists():
            return 0
        count = 0
        try:
            for _ in self.jsonl_path.open("r", encoding="utf-8"):
                count += 1
        except Exception:
            return 0
        return count

    def record(
        self,
        *,
        frame: Dict[str, Any],
        decoded: Dict[str, Any],
        parsed: Dict[str, Any],
    ) -> Path:
        """保存一个事件，返回写入的文件路径。"""
        self._seq += 1
        ts_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        record: Dict[str, Any] = {
            "timestamp": ts_utc,
            "seq": self._seq,
            "event_type": parsed.get("event_type"),
            "redReminder": parsed.get("redReminder"),
            "chat_id": parsed.get("chat_id"),
            "sender_id": parsed.get("sender_id"),
            "content": parsed.get("content"),
            # 候选值：Phase 0 结论中 order_id 无统一可靠来源，以下均为候选，
            # 可能存在多个，全部保留，不认定唯一 order_id。
            "candidate_order_ids": parsed.get("candidate_order_ids", []),
            "candidate_buyer_ids": parsed.get("candidate_buyer_ids", []),
            "candidate_item_ids": parsed.get("candidate_item_ids", []),
            "decoded": decoded,
            "raw_frame": frame,
        }

        file_path = self.log_dir / f"{self._seq:06d}_{ts_utc.replace(':', '').replace('+00:00', 'Z')}.json"
        file_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            summary = {
                k: record[k]
                for k in (
                    "timestamp",
                    "seq",
                    "event_type",
                    "redReminder",
                    "chat_id",
                    "sender_id",
                    "candidate_order_ids",
                    "candidate_buyer_ids",
                    "candidate_item_ids",
                )
            }
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")

        return file_path
