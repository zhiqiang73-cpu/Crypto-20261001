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
    # 同向有效交叉可分层加仓；1 表示沿用旧版「同向忽略」。
    # 它是硬上限，不是优化参数，防止趋势震荡中无限叠加杠杆。
    max_layers: int = 1
    # 每层风险比例；None 表示所有层沿用模块级 RISK_R。
    # BTC 15m 采用「首层 1%，后续层 0.5%」的金字塔风险预算。
    layer_risk_r: Optional[Tuple[float, ...]] = None


SPEC_15M = StrategySpec(
    id="kdj15",
    symbol="BTCUSDT",
    interval="15m",
    interval_ms=15 * 60 * 1000,
    tag="kdj",
    k_long_max=None,
    k_short_min=None,
    lookback=96,
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 首层做多 / 已多则同向加一层；"
                "死叉且 MACD 能量柱为负 → 首层做空 / 已空则同向加一层；"
                "方向背离丢弃不操作；有效反向则清空全部层后反手；最多 3 层",
    cold_start=False,
    label="BTC 15m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
    max_layers=3,
    layer_risk_r=(0.01, 0.005, 0.005),
)
SPEC_5M = StrategySpec(
    id="kdj5",
    symbol="BTCUSDT",
    interval="5m",
    interval_ms=5 * 60 * 1000,
    tag="kd5",
    k_long_max=None,
    k_short_min=None,
    lookback=288,
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 做多；死叉且能量柱为负 → 做空；"
                "方向背离的交叉丢弃不操作；不使用 K 极值过滤",
    cold_start=True,
    label="BTC 5m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
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
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 首层做多 / 已多则同向加一层；"
                "死叉且 MACD 能量柱为负 → 首层做空 / 已空则同向加一层；"
                "方向背离丢弃不操作；有效反向则清空全部层后反手；最多 3 层",
    cold_start=True,
    label="ETH 15m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
    max_layers=3,
    layer_risk_r=(0.01, 0.005, 0.005),
)
SPEC_ETH_5M = StrategySpec(
    id="eth5",
    symbol="ETHUSDT",
    interval="5m",
    interval_ms=5 * 60 * 1000,
    tag="e5",
    k_long_max=None,
    k_short_min=None,
    lookback=288,
    signal_rule="当根收盘金叉且 MACD 能量柱为正 → 做多；死叉且能量柱为负 → 做空；"
                "方向背离的交叉丢弃不操作；不使用 K 极值过滤",
    cold_start=True,
    label="ETH 5m",
    confirm_next=False,
    require_break=False,
    require_macd=True,
)
# 2026-10-03 用户决定：停用 5m 实盘账本（研究建议停用），只保留两条 15m。
# SPEC_5M / SPEC_ETH_5M 保留定义供回测与研究使用，但不参与实盘净仓。
SPECS = (SPEC_15M, SPEC_ETH_15M)
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


def entry_layers(entry: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回虚拟仓的逐层明细，并兼容旧版单层 ``entry`` 状态。

    旧运行状态只有 ``side/qty/px``，没有 ``layers``。升级后把它视作第 1 层，
    不重写历史状态；只有下一次有效同向信号才会落为显式分层结构。
    """
    if not isinstance(entry, dict):
        return []
    raw = entry.get("layers")
    if isinstance(raw, list):
        layers = [dict(x) for x in raw if isinstance(x, dict)]
        if layers:
            return layers
    try:
        return [dict(entry)] if float(entry.get("qty") or 0.0) > 0 else []
    except (TypeError, ValueError):
        return []


def layer_count(entry: Optional[Dict[str, Any]]) -> int:
    """当前虚拟仓层数（旧单层状态返回 1）。"""
    return len(entry_layers(entry))


def book_signed_qty(book: Dict[str, Any]) -> float:
    """策略账本的带方向数量；显式分层时逐层求和。"""
    entry = book.get("entry") or {}
    total = 0.0
    for layer in entry_layers(entry):
        try:
            qty = float(layer.get("qty") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        side = layer.get("side")
        if side in (1, "LONG", "long"):
            total += qty
        elif side in (-1, "SHORT", "short"):
            total -= qty
    return total


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


def book_margin(book: Dict[str, Any], leverage: float) -> float:
    """该虚拟账本占用的保证金（按各层自己的入场价折算）。

    逐层算而不是用均价：分层加仓时每层入场价不同，用均价会让「已占用」在
    新层加入前后漂移，组合预算的判断就不稳。
    """
    if leverage <= 0:
        return 0.0
    total = 0.0
    for layer in entry_layers((book or {}).get("entry")):
        try:
            qty = abs(float(layer.get("qty") or 0.0))
            px = float(layer.get("px") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and px > 0:
            total += qty * px / leverage
    return total


def other_symbol_margin(st: Dict[str, Any], symbol: str,
                        leverage: float) -> float:
    """除本标的以外，其它标的的虚拟账本已占用的保证金。

    组合级预算按「先到先得」分配：先建仓的标的先占额度，后来者只能用剩下的。
    """
    books = st.get("strategies") or {}
    total = 0.0
    for spec in SPECS:
        if spec.symbol == symbol:
            continue
        total += book_margin(books.get(spec.id) or {}, leverage)
    return total


def clear_symbol_books(st: Dict[str, Any], symbol: str) -> None:
    books = st.setdefault("strategies", {})
    for spec in specs_for_symbol(symbol):
        book = books.setdefault(spec.id, empty_book())
        book["entry"] = None


def reconcile_symbol_books(st: Dict[str, Any], symbol: str,
                           actual_signed: float, *,
                           min_qty: float = 0.001) -> Optional[str]:
    """把该标的的虚拟账本对齐到交易所**实际**净仓。返回说明；未改动返回 None。

    为什么需要：账本在委托**之前**就写入意图，而委托可能只成交一部分。此前只有
    「零成交」会回滚，部分成交会留下虚高账本 —— 2026-10-04 ETH 目标 -15.854、
    实际只成交到 -7.430，账本却一直记着 -15.854。

    差额只可能来自**最近一次下单**，所以从最新那一层往回削，语义正确：老层的
    成交价与层号都保持不变，或被整层移除。

    只处理「同向、实际比账本少」这一个方向。反手/交易所比账本多都属异常状态，
    不做猜测，返回说明请人工复核。
    """
    books = [st.setdefault("strategies", {}).setdefault(spec.id, empty_book())
             for spec in specs_for_symbol(symbol)]
    book_total = sum(book_signed_qty(b) for b in books)
    target = float(actual_signed)
    if abs(target - book_total) < min_qty:
        return None

    if abs(target) < min_qty:                      # 交易所说空仓
        if abs(book_total) < min_qty:
            return None
        clear_symbol_books(st, symbol)
        return (f"交易所已空仓，账本由 {book_total:+.4f} 归零")

    if book_total == 0 or (book_total > 0) != (target > 0):
        return (f"账本 {book_total:+.4f} 与实际 {target:+.4f} 方向不符，"
                f"不做猜测，请人工复核")
    if abs(target) > abs(book_total):
        return (f"实际 {target:+.4f} 多于账本 {book_total:+.4f}，"
                f"非本轮成交所致，请人工复核")

    shortfall = abs(book_total) - abs(target)
    for book in reversed(books):                   # 从最新加的那本开始削
        if shortfall < min_qty:
            break
        entry = book.get("entry")
        if not entry:
            continue
        layers = entry_layers(entry)
        want = 1 if book_signed_qty(book) > 0 else -1
        while layers and shortfall >= min_qty:
            top = layers[-1]
            qty = max(0.0, float(top.get("qty") or 0.0))
            take = min(qty, shortfall)
            left = qty - take
            if left < min_qty:
                layers.pop()
                shortfall -= qty
            else:
                top["qty"] = round(left, 8)
                shortfall -= take
        book["entry"] = _aggregate_layers(want, layers) if layers else None
    return (f"部分成交对齐：账本 {book_total:+.4f} → {target:+.4f}"
            f"（差额 {shortfall:.4f} 已从最新层扣除）")


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


def _aggregate_layers(want: int, layers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """将同向层合成旧调用方仍可识别的 ``entry`` 顶层视图。

    顶层 ``qty`` 与 ``px`` 分别是总数量和数量加权均价，确保净仓同步、面板与
    灾难止损继续使用正确的账户级数据；逐层细节完整保留在 ``layers``。
    """
    clean = [dict(layer) for layer in layers]
    total_qty = sum(max(0.0, float(layer.get("qty") or 0.0)) for layer in clean)
    if total_qty <= 0:
        raise ValueError("分层仓位总数量必须大于 0")
    weighted_px = sum(
        max(0.0, float(layer.get("qty") or 0.0)) * float(layer.get("px") or 0.0)
        for layer in clean
    ) / total_qty
    weighted_atr = sum(
        max(0.0, float(layer.get("qty") or 0.0)) * float(layer.get("atr") or 0.0)
        for layer in clean
    ) / total_qty
    # 最新层的信号解释作为顶层原因；所有历史原因仍在 layers 内可审计。
    merged = dict(clean[-1])
    merged.update({
        "side": want,
        "qty": total_qty,
        "px": weighted_px,
        "atr": weighted_atr,
        "layers": clean,
        "layer_count": len(clean),
    })
    return merged


def _new_layer(want: int, qty: float, px: float, atr: float, ms: int,
               meta: Optional[Dict[str, Any]], number: int) -> Dict[str, Any]:
    layer = _entry_record(want, qty, px, atr, ms, meta)
    layer["layer"] = number
    return layer


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
    max_layers: int = 1,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], str, bool]:
    """只改虚拟账本，不下单。返回 (动作, 备注, 是否变化)。

    有效同向信号可新增一层，最多 ``max_layers`` 层；方向背离在本函数外已经
    归零为无信号，因此不会加仓、平仓或反手。有效反向会先丢弃全部旧层，再以
    一层新方向仓位重建账本。
    """
    want = 1 if sig_long else (-1 if sig_short else 0)
    if want == 0:
        return None, "观察", False
    max_layers = max(1, int(max_layers))
    entry = book.get("entry")
    if entry is None:
        if block:
            return None, "风控闸门: 不开新仓", False
        if qty < min_qty:
            return None, f"数量不足 qty={qty:.4f}", False
        book["entry"] = _aggregate_layers(
            want, [_new_layer(want, qty, px, atr, ms, meta, 1)]
        )
        return ("开多" if want == 1 else "开空"), f"qty={qty:.4f}", True

    current_layers = entry_layers(entry)
    current_signed = book_signed_qty({"entry": entry})
    cur = 1 if current_signed > 0 else -1
    if cur == want:
        if block:
            return None, "风控闸门: 不加仓", False
        if len(current_layers) >= max_layers:
            return None, f"同向信号; 已达最大分层 {max_layers}层，不加仓", False
        if qty < min_qty:
            return None, f"同向信号; 加仓数量不足 {qty:.4f}", False
        number = len(current_layers) + 1
        current_layers.append(_new_layer(want, qty, px, atr, ms, meta, number))
        book["entry"] = _aggregate_layers(want, current_layers)
        action = "加多" if want == 1 else "加空"
        return action, (f"第{number}/{max_layers}层; 本层 {qty:.4f}; "
                        f"累计 {book['entry']['qty']:.4f}"), True

    cleared = max(1, len(current_layers))
    book["entry"] = None
    close_action = "平仓" if max_layers == 1 else f"清仓{cleared}层"
    if block:
        return close_action, f"{close_action}; 风控阻止反手开新仓", True
    if qty < min_qty:
        return close_action, f"{close_action}; 反手数量不足 {qty:.4f}", True
    book["entry"] = _aggregate_layers(
        want, [_new_layer(want, qty, px, atr, ms, meta, 1)]
    )
    action = f"{close_action}并开多" if want == 1 else f"{close_action}并开空"
    return action, f"{close_action}; 反手第1/{max_layers}层 {qty:.4f}", True


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
        "layer_count": layer_count(book.get("entry")),
        "max_layers": spec.max_layers if spec else 1,
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
            "layer_count": layer_count(entry),
            "max_layers": spec.max_layers,
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
