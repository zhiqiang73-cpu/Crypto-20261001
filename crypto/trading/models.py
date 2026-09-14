"""交易相关数据结构."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PositionInfo:
    symbol: str
    side: str                 # "LONG" | "SHORT" | "FLAT"
    quantity: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: int = 1
    mark_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AccountBalance:
    total_wallet_balance: float = 0.0
    available_balance: float = 0.0
    total_unrealized_pnl: float = 0.0
    asset: str = "USDT"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    symbol: str = ""
    side: str = ""            # BUY / SELL
    position_side: str = ""   # LONG / SHORT
    quantity: float = 0.0
    avg_price: float = 0.0
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HorizonPosition:
    horizon: str              # short_term / long_term
    side: str                 # LONG / SHORT / FLAT
    entry_price: float = 0.0
    quantity: float = 0.0
    leverage: int = 1
    opened_at_ms: int = 0
    order_id: str = ""
    trade_id: str = ""
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    # V8 分阶段出场
    original_quantity: float = 0.0   # 开仓初始数量 (减仓后不变)
    remaining_pct: float = 1.0       # 剩余仓位占比 (0~1)
    peak_price: float = 0.0          # 持仓期间极值 (多=最高 / 空=最低)
    trailing_stop_price: float = 0.0 # 追踪止盈触发价
    tp_levels_hit: int = 0           # 已触发止盈阶段数 (0/1/2)
    sl_price: float = 0.0            # 硬止损价
    entry_cs: float = 0.0            # 入场时 CS
    entry_atr: float = 0.0           # 入场时 ATR (价格单位) — 定仓与止损共用
    risk_usdt: float = 0.0           # 本笔计划风险金额
    cs_decay_done: bool = False      # CS 衰减减仓是否已执行
    trailing_tightened: bool = False # Predict.fun 突变后是否收紧追踪

    def is_flat(self) -> bool:
        return self.side in ("FLAT", "", None) or float(self.quantity or 0) <= 0

    def pnl_pct(self, mark: Optional[float] = None) -> float:
        """未杠杆名义 PnL 百分比 (相对开仓价)."""
        px = mark if mark is not None else self.mark_price
        if self.entry_price <= 0 or px <= 0:
            return 0.0
        raw = (px - self.entry_price) / self.entry_price
        return raw if self.side == "LONG" else -raw

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExitAction:
    """出场检查器产出的动作指令."""
    kind: str                 # full_close / partial_close / tighten_trailing
    close_pct: float = 1.0    # 相对当前剩余仓位的平仓比例
    reason: str = ""
    new_trailing_pct: Optional[float] = None  # tighten 时用

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TradeAction:
    action: str               # open / close / reverse_close / reverse_open / partial_close
    horizon: str
    side: str                 # LONG / SHORT
    quantity: float
    price: float
    leverage: int
    decision: str
    reason: str = ""
    order: Optional[OrderResult] = None
    trade_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.order is not None:
            d["order"] = self.order.to_dict()
        return d
