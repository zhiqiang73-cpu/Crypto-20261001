"""真实成交账本 — 与信号研究 (settle/MFE-MAE) 严格分离."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.review import TRADE_LEDGER_PATH
from models.review import now_ms

logger = logging.getLogger(__name__)


@dataclass
class TradeLedgerEntry:
    """真实成交或审计事件."""

    entry_id: str
    trade_id: str
    ts_ms: int
    horizon: str
    action: str
    side: str
    quantity: float
    price: float
    fee_usdt: float = 0.0
    fee_unknown: bool = False
    funding_usdt: float = 0.0
    realized_pnl_usdt: Optional[float] = None
    is_internal_match: bool = False
    rejected: bool = False
    is_audit: bool = False
    entry_kind: str = "fill"  # fill | reject | sync | audit
    order_id: str = ""
    client_order_id: str = ""
    exchange_net_after: Optional[float] = None
    note: str = ""
    config_version: Optional[str] = None
    content_hash: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SignalResearchRecord:
    """信号研究 — first-touch / MFE / MAE; 不代表真实成交 PnL."""

    signal_id: str
    opened_at_ms: int
    horizon: str
    decision: str
    entry_price: float
    mfe_atr: Optional[float] = None
    mae_atr: Optional[float] = None
    first_touch: Optional[str] = None
    status: str = "pending"
    note: str = ""
    is_research_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TradeLedger:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else TRADE_LEDGER_PATH
        self._rows: List[dict] = []
        self._seen_fill_keys: set = set()
        self._load()

    def _load(self) -> None:
        self._rows = []
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    row = json.loads(line)
                    self._rows.append(row)
                    if row.get("entry_kind") == "fill":
                        self._seen_fill_keys.add(self._fill_key_from_row(row))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("trade ledger load: %s", exc)

    @staticmethod
    def _fill_key_from_row(row: dict) -> tuple:
        meta = row.get("meta") or {}
        ex_tid = meta.get("exchange_trade_id") or meta.get("trade_id") or row.get("exchange_trade_id")
        if ex_tid:
            return ("xtid", row.get("order_id") or row.get("client_order_id"), str(ex_tid))
        # 无成交 id 时：订单 + 累计成交水位（禁止用接收时间）
        cum = meta.get("cum_filled_qty")
        if cum is not None:
            return ("cum", row.get("client_order_id"), row.get("order_id"), round(float(cum), 8))
        return (
            "qty",
            row.get("client_order_id"),
            row.get("order_id"),
            round(float(row.get("quantity") or 0), 8),
            row.get("action"),
        )

    @staticmethod
    def _fill_key(entry: TradeLedgerEntry) -> tuple:
        return TradeLedger._fill_key_from_row(entry.to_dict())

    def append(self, entry: TradeLedgerEntry) -> None:
        if entry.entry_kind == "fill":
            key = self._fill_key(entry)
            if key in self._seen_fill_keys:
                return  # 幂等
            self._seen_fill_keys.add(key)
        row = entry.to_dict()
        self._rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 50) -> List[dict]:
        return list(reversed(self._rows[-limit:]))

    @staticmethod
    def new_id(prefix: str = "tl") -> str:
        return f"{prefix}_{now_ms()}"
