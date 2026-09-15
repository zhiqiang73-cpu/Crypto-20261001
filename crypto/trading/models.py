"""交易相关数据结构."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class OrderState(str, Enum):
    """订单生命周期 — ACK/HTTP200/账户有仓都不能单独证明本笔已完整成交."""

    INTENT = "intent"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SystemHealth(str, Enum):
    NOT_READY = "not_ready"     # 启动/无有效行情
    NORMAL = "normal"
    DEGRADED = "degraded"
    REDUCE_ONLY = "reduce_only"
    EMERGENCY = "emergency"
    HALTED = "halted"


class DataValidity(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    WARMING_UP = "warming_up"


@dataclass
class ManagedOrder:
    """可追踪的订单 — 以 client_order_id 为幂等键."""

    client_order_id: str
    state: OrderState = OrderState.INTENT
    exchange_order_id: str = ""
    symbol: str = ""
    side: str = ""              # BUY / SELL
    position_side: str = ""     # LONG / SHORT
    requested_qty: float = 0.0
    submitted_qty: float = 0.0
    cum_filled_qty: float = 0.0
    last_fill_qty: float = 0.0
    quantity: float = 0.0       # 兼容 = submitted
    filled_qty: float = 0.0     # 兼容 = cum_filled
    avg_price: float = 0.0
    reduce_only: bool = False
    is_stop: bool = False
    is_algo: bool = False
    stop_price: float = 0.0
    algo_id: str = ""
    horizon: str = ""
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_order_result(self) -> "OrderResult":
        filled = self.cum_filled_qty or self.filled_qty
        submitted = self.submitted_qty or self.quantity
        ok = self.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) and filled > 0
        return OrderResult(
            ok=ok,
            order_id=self.exchange_order_id or self.client_order_id,
            symbol=self.symbol,
            side=self.side,
            position_side=self.position_side,
            quantity=filled,
            requested_qty=self.requested_qty,
            submitted_qty=submitted,
            cum_filled_qty=filled,
            last_fill_qty=self.last_fill_qty,
            avg_price=self.avg_price,
            status=self.state.value,
            raw=dict(self.raw),
            error=self.error,
            client_order_id=self.client_order_id,
            order_state=self.state.value,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


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
    quantity: float = 0.0     # 兼容：已成交量 (cum_filled)
    requested_qty: float = 0.0
    submitted_qty: float = 0.0
    cum_filled_qty: float = 0.0
    last_fill_qty: float = 0.0
    avg_price: float = 0.0
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    client_order_id: str = ""
    order_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FillAllocation:
    """策略层成交分配 — 不得把净仓变化订单总量直接当作某策略成交量."""

    horizon: str
    side: str                 # LONG / SHORT
    quantity: float
    price: float
    is_internal_match: bool = False
    fee_usdt: float = 0.0
    order_id: str = ""

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
    exit_incomplete: bool = False     # 减仓/全平部分成交未完成
    pending_exit_qty: float = 0.0     # 仍待成交的减仓意图
    pending_order_cid: str = ""       # 在途订单 client id
    trailing_activated: bool = False  # 追踪止盈是否已激活(与 ExitChecker 共用)
    config_snapshot: Optional[Dict[str, Any]] = None  # 开仓时配置

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
