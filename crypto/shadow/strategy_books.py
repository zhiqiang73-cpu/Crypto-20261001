"""多策略虚拟账本：每条策略各记各的仓，每个标的只下自己的净额。

同一标的的 15m 与 5m 共用该标的单向账户。两套运行器同时下单会抢仓，
所以按标的合成净仓再委托。BTC 与 ETH 绝不混成一个数字。
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class StrategySpec:
    id: str
    symbol: str
    interval: str
    interval_ms: int
    tag: str
    k_long_max: Optional[float]
    k_short_min: Optional[float]
    lookback: int
    signal_rule: str
    cold_start: bool
    label: str
    confirm_next: bool = False
    require_break: bool = False
    require_macd: bool = False


SPEC_15M = StrategySpec(
    id="kdj15",
    symbol="BTCUSDT",
    interval="15m",
    interval_ms=15 * 60 * 1000,
    tag="kdj",
    k_long_max=None,
    k_short_min=None,
    lookback=96,
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 做多；死叉且能量柱为负 → 做空；"
                "方向背离的交叉丢弃不操作；不使用 K 极值过滤",
    cold_start=False,
    label="BTC 15m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
)
SPEC_5M = StrategySpec(
    id="kdj5",
    symbol="BTCUSDT",
    interval="5m",
    interval_ms=5 * 60 * 1000,
    tag="kd5",
    k_long_max=30.0,
    k_short_min=70.0,
    lookback=288,
    signal_rule="金叉且 K<30 做多 / 死叉且 K>70 做空",
    cold_start=True,
    label="BTC 5m",
)
SPEC_ETH_15M = StrategySpec(
    id="eth15",
    symbol="ETHUSDT",
    interval="15m",
    interval_ms=15 * 60 * 1000,
    tag="e15",
    k_long_max=None,
    k_short_min=None,
    lookback=96,
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 做多；死叉且能量柱为负 → 做空；"
                "方向背离的交叉丢弃不操作；不使用 K 极值过滤",
    cold_start=True,
    label="ETH 15m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
)
SPEC_ETH_5M = StrategySpec(
    id="eth5",
    symbol="ETHUSDT",
    interval="5m",
    interval_ms=5 * 60 * 1000,
    tag="e5",
    k_long_max=30.0,
    k_short_min=70.0,
    lookback=288,
    signal_rule="金叉且 K<30 做多 / 死叉且 K>70 做空",
    cold_start=True,
    label="ETH 5m",
)
SPECS = (SPEC_15M, SPEC_5M, SPEC_ETH_15M, SPEC_ETH_5M)
SPEC_BY_ID = {spec.id: spec for spec in SPECS}
TRADE_SYMBOLS = ("BTCUSDT", "ETHUSDT")

RUNTIME_KEY_BY_STRATEGY_ID = {
    "deployed_kdj_extreme_v1": "kdj15",
    "deployed_kdj_5m_extreme_v1": "kdj5",
    "deployed_kdj_eth_extreme_v1": "eth15",
    "deployed_kdj_eth_5m_extreme_v1": "eth5",
}


def symbol_short(symbol: str) -> str:
    return str(symbol or "").replace("USDT", "") or str(symbol or "")


def specs_for_symbol(symbol: str) -> Tuple[StrategySpec, ...]:
    return tuple(spec for spec in SPECS if spec.symbol == symbol)


def empty_book() -> Dict[str, Any]:
    return {"last_ts": 0, "entry": None, "missed_bars": 0,
            "missed_signals": 0, "armed": False}


def migrate_state(st: Dict[str, Any]) -> Dict[str, Any]:
    """把旧的单策略状态迁进 strategies.kdj15；新标的/5m 冷启动，不追溯旧 K 线。"""
    books = st.setdefault("strategies", {})
    if "kdj15" not in books:
        books["kdj15"] = {
            "last_ts": int(st.get("last_ts") or 0),
            "entry": st.get("entry"),
            "missed_bars": int(st.get("missed_bars") or 0),
            "missed_signals": int(st.get("missed_signals") or 0),
            "armed": True,
        }
    else:
        books["kdj15"].setdefault("armed", True)
        books["kdj15"].setdefault("missed_bars", 0)
        books["kdj15"].setdefault("missed_signals", 0)
    for spec in SPECS:
        if spec.id == "kdj15":
            continue
        if spec.id not in books:
            books[spec.id] = empty_book()
    return st


CONTRA_5M_MULT = 0.5


def book_signed_qty(book: Dict[str, Any]) -> float:
    entry = book.get("entry") or {}
    qty = float(entry.get("qty") or 0.0)
    if qty <= 0:
        return 0.0
    side = entry.get("side")
    if side in (1, "LONG", "long"):
        return qty
    if side in (-1, "SHORT", "short"):
        return -qty
    return 0.0


def trend_side(st: Dict[str, Any], symbol: str) -> int:
    """同标的 15m 虚拟仓方向：多=1，空=-1，空仓=0。"""
    books = st.get("strategies") or {}
    for spec in specs_for_symbol(symbol):
        if spec.interval != "15m":
            continue
        qty = book_signed_qty(books.get(spec.id) or {})
        if qty > 0:
            return 1
        if qty < 0:
            return -1
        return 0
    return 0


def contra_5m_qty(qty: float, *, interval: str, want: int, trend: int,
                  step: float = 0.001) -> Tuple[float, str]:
    """5m 逆着同标的 15m 时仓位减半。15m 自己不减；15m 空仓则 5m 满仓。"""
    if qty <= 0 or interval != "5m" or want == 0 or trend == 0:
        return qty, ""
    if want * trend > 0:
        return qty, ""
    halved = math.floor((qty * CONTRA_5M_MULT) / step + 1e-12) * step
    if halved <= 0:
        return 0.0, "逆15m减半后数量不足"
    return halved, "逆15m，仓位减半"


def desired_net(st: Dict[str, Any], symbol: str = "BTCUSDT") -> float:
    """只加总同一个标的的虚拟仓。BTC 数量和 ETH 数量不能相加。"""
    total = 0.0
    books = st.get("strategies") or {}
    for spec in specs_for_symbol(symbol):
        total += book_signed_qty(books.get(spec.id) or {})
    return total


def desired_nets(st: Dict[str, Any]) -> Dict[str, float]:
    return {symbol: desired_net(st, symbol) for symbol in TRADE_SYMBOLS}


def clear_symbol_books(st: Dict[str, Any], symbol: str) -> None:
    books = st.setdefault("strategies", {})
    for spec in specs_for_symbol(symbol):
        book = books.setdefault(spec.id, empty_book())
        book["entry"] = None


def signal_reason(spec: StrategySpec, *, gold: bool, dead: bool,
                  k: Optional[float] = None) -> str:
    """人话原因：哪条规则在这一根上成立。"""
    ktxt = ""
    if k is not None and math.isfinite(float(k)):
        ktxt = f"（K={float(k):.1f}）"
    if gold:
        if spec.k_long_max is None:
            return f"金叉做多{ktxt}"
        return f"金叉且 K<{spec.k_long_max:.0f} 做多{ktxt}"
    if dead:
        if spec.k_short_min is None:
            return f"死叉做空{ktxt}"
        return f"死叉且 K>{spec.k_short_min:.0f} 做空{ktxt}"
    return "未知信号"


def _entry_record(want: int, qty: float, px: float, atr: float, ms: int,
                  meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rec = {"side": want, "px": px, "qty": qty, "atr": atr, "ms": ms}
    if meta:
        rec.update(meta)
    return rec


def apply_virtual_signal(
    book: Dict[str, Any],
    *,
    sig_long: bool,
    sig_short: bool,
    qty: float,
    px: float,
    atr: float,
    ms: int,
    block: bool,
    min_qty: float,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], str, bool]:
    """只改虚拟账本，不下单。返回 (动作, 备注, 是否变化)。"""
    want = 1 if sig_long else (-1 if sig_short else 0)
    if want == 0:
        return None, "观察", False
    entry = book.get("entry")
    if entry is None:
        if block:
            return None, "风控闸门: 不开新仓", False
        if qty < min_qty:
            return None, f"数量不足 qty={qty:.4f}", False
        book["entry"] = _entry_record(want, qty, px, atr, ms, meta)
        return ("开多" if want == 1 else "开空"), f"qty={qty:.4f}", True

    cur = 1 if float(entry.get("side") or 0) > 0 else -1
    if cur == want:
        return None, "同向持仓", False
    book["entry"] = None
    if block:
        return "平仓", "平仓; 风控阻止反手开新仓", True
    if qty < min_qty:
        return "平仓", f"平仓; 反手数量不足 {qty:.4f}", True
    book["entry"] = _entry_record(want, qty, px, atr, ms, meta)
    action = "平仓并开多" if want == 1 else "平仓并开空"
    return action, f"平仓; 反手 {qty:.4f}", True


def runtime_view(st: Dict[str, Any], strategy_id: str,
                 runtime_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """面板按策略卡挂运行态：只给这一条的虚拟仓，外加同标的账户净仓。"""
    key = runtime_key or RUNTIME_KEY_BY_STRATEGY_ID.get(strategy_id)
    books = st.get("strategies") or {}
    if not key or key not in books:
        return None
    book = books[key]
    spec = SPEC_BY_ID.get(key)
    symbol = spec.symbol if spec else "BTCUSDT"
    return {
        "last_ts": book.get("last_ts"),
        "entry": book.get("entry"),
        "missed_bars": int(book.get("missed_bars") or 0),
        "missed_signals": int(book.get("missed_signals") or 0),
        "armed": bool(book.get("armed")),
        "halted": bool(st.get("halted")),
        "desired_net": desired_net(st, symbol),
        "symbol": symbol,
        "runtime_key": key,
    }


def _bar_utc(ms: Any) -> str:
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _action_belongs(action: str, spec: StrategySpec) -> bool:
    """交易日志动作是否属于这条策略。新行带标的前缀；旧 BTC 行只有周期。"""
    tagged = f"{symbol_short(spec.symbol)} {spec.interval}"
    if action.startswith(tagged):
        return True
    if spec.symbol == "BTCUSDT" and action.startswith(spec.interval):
        return not action.startswith(("BTC ", "ETH "))
    return False


def _csv_open_hints(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """用交易日志补全旧虚拟仓缺少的原因。按策略取最后一次开仓行。"""
    hints: Dict[str, Dict[str, Any]] = {}
    if not path or not os.path.exists(path):
        return hints
    try:
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    except Exception:
        return hints
    for row in rows[1:]:
        if len(row) < 8:
            continue
        action = row[1]
        for spec in SPECS:
            if not _action_belongs(action, spec):
                continue
            if "开多" not in action and "开空" not in action:
                continue
            gold = "开多" in action
            dead = "开空" in action
            try:
                k_val = float(row[6]) if row[6] else None
            except ValueError:
                k_val = None
            try:
                d_val = float(row[7]) if row[7] else None
            except ValueError:
                d_val = None
            hint = {
                "reason": signal_reason(spec, gold=gold, dead=dead, k=k_val),
                "k": k_val,
                "d": d_val,
                "interval": spec.interval,
                "strategy_id": spec.id,
                "symbol": spec.symbol,
            }
            hints[(spec.id, row[0])] = hint
            hints[spec.id] = hint
    return hints


def position_sources(st: Dict[str, Any],
                     trade_log_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """当前每条策略虚拟仓的开仓归因，供面板「当前仓位」展示。"""
    hints = _csv_open_hints(trade_log_path)
    books = st.get("strategies") or {}
    out: List[Dict[str, Any]] = []
    for spec in SPECS:
        book = books.get(spec.id) or {}
        entry = book.get("entry") or {}
        qty = float(entry.get("qty") or 0.0)
        if qty <= 0:
            continue
        side_n = float(entry.get("side") or 0)
        if side_n == 0:
            continue
        bar_utc = _bar_utc(entry.get("ms"))
        hint = hints.get((spec.id, bar_utc)) or hints.get(spec.id) or {}
        reason = entry.get("reason") or hint.get("reason") or spec.signal_rule
        k_val = entry.get("k", hint.get("k"))
        d_val = entry.get("d", hint.get("d"))
        out.append({
            "strategy_id": spec.id,
            "name": spec.label,
            "symbol": spec.symbol,
            "symbol_short": symbol_short(spec.symbol),
            "interval": spec.interval,
            "side": "LONG" if side_n > 0 else "SHORT",
            "qty": qty,
            "px": float(entry.get("px") or 0.0),
            "bar_ms": int(entry.get("ms") or 0),
            "bar_utc": bar_utc,
            "reason": reason,
            "k": None if k_val is None else float(k_val),
            "d": None if d_val is None else float(d_val),
            "signal_rule": spec.signal_rule,
        })
    return out


def reduce_only_for_delta(exchange_signed: float, desired: float, min_qty: float) -> bool:
    """净仓只减不增、或不翻向时，用 reduceOnly，避免超开。"""
    if abs(desired) < min_qty:
        return True
    if exchange_signed == 0:
        return False
    if desired * exchange_signed < 0:
        return False
    return abs(desired) < abs(exchange_signed) - 1e-12
