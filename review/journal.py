"""交易档案存储 — JSONL, 追加为主, 更新时整档重写 (记录量小, 千级以内).

为什么用 JSONL 而不是 SQLite:
  * 复盘档案是「只增不改」为主, 人可读、可直接 diff、可手工修补;
  * 结算与备注更新频率低, 整档重写完全够用, 且无需额外依赖。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config.review import JOURNAL_PATH
from models.review import SettleStatus, TradeRecord

logger = logging.getLogger(__name__)


def new_trade_id(opened_at_ms: int) -> str:
    """可排序、可读的档案号: T<时间戳>-<短随机>."""
    return f"T{opened_at_ms}-{uuid.uuid4().hex[:6]}"


class TradeJournal:
    """交易档案库."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else JOURNAL_PATH
        self._records: Dict[str, TradeRecord] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 读写
    def load(self, force: bool = False) -> "TradeJournal":
        if self._loaded and not force:
            return self
        self._records = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = TradeRecord.from_dict(json.loads(line))
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        # 单行损坏不该拖垮整档, 记一条告警继续读
                        logger.warning("journal: 跳过损坏行 %s: %s", lineno, exc)
                        continue
                    if not rec.trade_id:
                        rec.trade_id = new_trade_id(rec.opened_at_ms)
                    self._records[rec.trade_id] = rec
        self._loaded = True
        return self

    def _flush(self) -> None:
        """整档原子重写 (临时文件 + os.replace, 避免半截文件)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for rec in self._sorted():
                    fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _sorted(self) -> List[TradeRecord]:
        return sorted(self._records.values(), key=lambda r: r.opened_at_ms)

    # ------------------------------------------------------------------ 增删改查
    def append(self, record: TradeRecord) -> TradeRecord:
        self.load()
        if not record.trade_id:
            record.trade_id = new_trade_id(record.opened_at_ms)
        self._records[record.trade_id] = record
        self._flush()
        return record

    def append_many(self, records: Iterable[TradeRecord]) -> int:
        """批量写入, 只落盘一次.

        逐条 append 每次都整档重写, 批量导入 (演练/回补历史) 会退化成 O(n²)。
        """
        self.load()
        n = 0
        for record in records:
            if not record.trade_id:
                record.trade_id = new_trade_id(record.opened_at_ms)
            self._records[record.trade_id] = record
            n += 1
        if n:
            self._flush()
        return n

    def update(self, record: TradeRecord) -> TradeRecord:
        self.load()
        self._records[record.trade_id] = record
        self._flush()
        return record

    def get(self, trade_id: str) -> Optional[TradeRecord]:
        self.load()
        return self._records.get(trade_id)

    def all(self) -> List[TradeRecord]:
        self.load()
        return self._sorted()

    def filter(
        self,
        status: Optional[str] = None,
        horizon: Optional[str] = None,
        limit: Optional[int] = None,
        errors_only: bool = False,
    ) -> List[TradeRecord]:
        recs = self.all()
        if status:
            recs = [r for r in recs if r.status == status]
        if horizon:
            recs = [r for r in recs if r.horizon == horizon]
        if errors_only:
            recs = [r for r in recs if r.status == SettleStatus.WRONG.value]
        recs = list(reversed(recs))                 # 默认最新在前
        if limit:
            recs = recs[:limit]
        return recs

    def pending(self) -> List[TradeRecord]:
        self.load()
        return [r for r in self._sorted() if r.status == SettleStatus.PENDING.value]

    def errors(self) -> List[TradeRecord]:
        self.load()
        return [r for r in self._sorted() if r.status == SettleStatus.WRONG.value]

    def __len__(self) -> int:
        self.load()
        return len(self._records)


def records_from_dicts(rows: Iterable[Dict[str, Any]]) -> List[TradeRecord]:
    return [TradeRecord.from_dict(r) for r in rows]
