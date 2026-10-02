"""影子模式引擎 —— 严格按用户规格逐字实现, 不增删任何条件或参数.

规格要点 (全部硬编码为常量, 不对外暴露为可调项):
    信号周期 15m, 风险参数周期 1H; 只用已收盘 K 线。
    做多 = 金叉 且 K_t < 30
    做空 = 死叉 且 K_t > 70
    宽松做多 = 金叉 且 最近 3 根内 min(K) < 30   (仅记录)
    宽松做空 = 死叉 且 最近 3 根内 max(K) > 70   (仅记录)
    出场 A (执行): 对侧【完整信号】→ 平仓并反手
    出场 B (并行记录, 不执行): 对侧【裸交叉】→ 平仓
    仓位: qty = 权益 × r_eff ÷ (k × ATR_1H), r=0.01, k=2, 向下取整到 stepSize
    布林闸门: fee_points = 0.001×现价; 目标距离 = (UP−LB)/2; 倍数 = 目标距离/fee_points
              倍数≥3 → r_eff = r;  2≤倍数<3 → r_eff = r/2;  倍数<2 → 不开仓仅记录
    风控: 单方向单仓位; 10x 逐仓 (仅保证金占用); 日亏≥3% 停止当日开新仓;
          累计回撤≥10% 全部停止; 灾难止损 浮亏≥3×ATR_1H → 强制市价平仓

【实现假设, 均已标注, 未改变任何规则本身】
  * 现价取信号 K 线 t 的收盘价 (决策时刻可知的最后一个价)。
  * 灾难止损用 K 线内的 high/low 触发, 成交在触发价 (即"触发时的市价")。
  * 手续费 0.05%/边, 与规格 fee_points = 0.001×价 (双边 0.10%) 一致。
  * 权益初始值可配置, 默认 1000 USDT。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from shadow.indicators import atr_wilder, boll, kdj

# --------------------------------------------------------------------------- 规格常量 (禁止修改)
KDJ_N, KDJ_M1, KDJ_M2 = 9, 3, 3
ATR_PERIOD = 14
BOLL_N, BOLL_K = 20, 2.0

K_LONG_MAX = 30.0        # 做多要求 K_t < 30
K_SHORT_MIN = 70.0       # 做空要求 K_t > 70
LOOSE_LOOKBACK = 3       # 宽松版: 最近 3 根内

# ---------------------------------------------------------------------------
# 仓位拨盘 (用户 2026-10-02 指令: 可以增加仓位)
#
#   数量     = 权益 × RISK_R / (ATR_MULT_K × ATR_1H)     向下取整到 0.001
#   等效杠杆 = RISK_R × 现价 / (ATR_MULT_K × ATR_1H) ≈ RISK_R × 82
#              (现价 84,000 / ATR_1H 520 时; ATR 越小杠杆越高)
#   RISK_R 的含义 = 价格反向走 2×ATR_1H 时, 亏掉权益的百分之几
#
#   RISK_R   等效杠杆   反向 2×ATR = 亏   该笔(2026-10-01, 毛利+2.86)净盈亏
#   0.01      0.82x        -1%            +1.27 USDT   ← 原值
#   0.02      1.63x        -2%            +2.54 USDT
#   0.03      2.45x        -3%            +3.81 USDT   ← 现用
#   0.05      4.08x        -5%            +6.35 USDT
#   0.08      6.53x        -8%           +10.2  USDT
#   0.12      9.80x       -12%           +15.2  USDT   ← LEVERAGE=10 的天花板
#
# 注意: 最后一列是**单笔实例**(n=1), 用来说明仓位是线性放大器, 不代表期望值。
# 天花板来自 LEVERAGE=10 逐仓: 再往上会因保证金不足被交易所拒单。
# ---------------------------------------------------------------------------
RISK_R = 0.03            # 单笔风险比例 r
ATR_MULT_K = 2.0         # ATR 倍数 k

GATE_FEE_RATE = 0.001    # fee_points = 0.001 × 现价
GATE_STRONG = 3.0        # 倍数 ≥ 3 → r_eff = r
GATE_WEAK = 2.0          # 2 ≤ 倍数 < 3 → r_eff = r/2; < 2 → 不开仓

# 用户 2026-10-01 指令: 去掉布林带闸门。
# True  = 按原规格启用闸门;  False = 关闭闸门, r_eff 恒为 RISK_R。
# 带宽/目标距离/倍数仍照常计算并写入日志, 便于事后对比。
BOLL_GATE_ENABLED = False

LEVERAGE = 10            # 固定 10x 逐仓 (仅保证金占用)
DAILY_LOSS_LIMIT = 0.03  # 单日亏损 ≥ 权益 3% → 当日停止开新仓
MAX_DRAWDOWN = 0.10      # 累计回撤 ≥ 权益 10% → 全部停止
DISASTER_ATR = 3.0       # 浮亏 ≥ 3 × ATR_1H → 强制市价平仓

FEE_PER_SIDE = 0.0005    # taker 0.05%/边
STEP_SIZE = 0.001        # LOT_SIZE.stepSize
MIN_QTY = 0.001          # LOT_SIZE.minQty
MIN_NOTIONAL = 50.0      # MIN_NOTIONAL.notional


def floor_step(qty: float) -> float:
    """按交易所最小变动单位向下取整。"""
    if qty <= 0:
        return 0.0
    return math.floor(qty / STEP_SIZE) * STEP_SIZE


@dataclass
class ShadowConfig:
    equity0: float = 1000.0


@dataclass
class OpenPosition:
    side: int               # 1 多 / -1 空
    entry_ms: int
    entry_px: float
    qty: float
    fee: float
    k_at_entry: float
    d_at_entry: float
    atr_at_entry: float
    bandwidth_at_entry: float
    multiple_at_entry: float
    fee_points_at_entry: float


@dataclass
class Trade:
    mode: str
    side: int
    entry_ms: int
    entry_px: float
    qty: float
    entry_fee: float
    exit_ms: int = 0
    exit_px: float = 0.0
    exit_fee: float = 0.0
    hold_min: float = 0.0
    gross: float = 0.0
    net: float = 0.0
    k_at_entry: float = 0.0
    d_at_entry: float = 0.0
    atr_at_entry: float = 0.0
    bandwidth_at_entry: float = 0.0
    multiple_at_entry: float = 0.0
    fee_points_at_entry: float = 0.0
    exit_reason: str = ""


@dataclass
class BarRow:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float
    k: float
    d: float
    j: float
    atr_1h: float
    up: float
    mb: float
    lb: float
    bandwidth: float
    target_dist: float
    multiple: float
    gold_cross: bool
    dead_cross: bool
    sig_long: bool
    sig_short: bool
    loose_long: bool
    loose_short: bool
    pos_side: int
    pos_qty: float


@dataclass
class ShadowResult:
    bars: List[BarRow] = field(default_factory=list)
    trades_a: List[Trade] = field(default_factory=list)
    trades_b: List[Trade] = field(default_factory=list)
    skips: List[dict] = field(default_factory=list)
    halts: List[dict] = field(default_factory=list)
    final_equity_a: float = 0.0
    final_equity_b: float = 0.0
    peak_a: float = 0.0
    peak_b: float = 0.0


def align_atr_1h(bars15_ms: np.ndarray, bars1h_ms: np.ndarray,
                 atr1h: np.ndarray) -> np.ndarray:
    """把 1H 的 ATR 对齐到每根 15m K 线: 取【已收盘】的最后一根 1H。

    15m K 线 t 收盘于 bars15_ms[t] + 15min; 1H K 线收盘于 bars1h_ms[j] + 60min。
    使用 bars1h_ms[j] + 60min ≤ bars15_ms[t] + 15min 的最后一根。
    """
    close15 = bars15_ms + 15 * 60 * 1000
    close1h = bars1h_ms + 60 * 60 * 1000
    idx = np.searchsorted(close1h, close15, side="right") - 1
    out = np.full(len(bars15_ms), np.nan, dtype=np.float64)
    ok = idx >= 0
    out[ok] = atr1h[idx[ok]]
    return out


def run_shadow(bars15: Dict[str, np.ndarray], bars1h: Dict[str, np.ndarray],
               cfg: Optional[ShadowConfig] = None) -> ShadowResult:
    cfg = cfg or ShadowConfig()
    n = len(bars15["close"])
    o, h, l, c, v = (bars15[x] for x in ("open", "high", "low", "close", "volume"))
    ts = bars15["ts"]

    k, d, j = kdj(h, l, c, KDJ_N, KDJ_M1, KDJ_M2)
    mb, up, lb, _sd = boll(c, BOLL_N, BOLL_K)
    atr1h_aligned = align_atr_1h(ts, bars1h["ts"],
                                 atr_wilder(bars1h["high"], bars1h["low"],
                                            bars1h["close"], ATR_PERIOD))

    res = ShadowResult()

    # ---- 模式 A (执行) 状态
    pos_a: Optional[OpenPosition] = None
    equity_a = cfg.equity0
    peak_a = cfg.equity0
    day_key_a = None
    day_start_eq_a = cfg.equity0
    halted_a = False
    # ---- 模式 B (并行记录, 不执行) 状态
    pos_b: Optional[OpenPosition] = None
    equity_b = cfg.equity0
    peak_b = cfg.equity0
    day_key_b = None
    day_start_eq_b = cfg.equity0

    def close_pos(pos: OpenPosition, ms: int, px: float, reason: str,
                  mode: str) -> float:
        gross = (px - pos.entry_px) * pos.qty * pos.side
        exit_fee = px * pos.qty * FEE_PER_SIDE
        net = gross - pos.fee - exit_fee
        t = Trade(
            mode=mode, side=pos.side, entry_ms=pos.entry_ms, entry_px=pos.entry_px,
            qty=pos.qty, entry_fee=pos.fee, exit_ms=ms, exit_px=px,
            exit_fee=exit_fee, hold_min=(ms - pos.entry_ms) / 60000.0,
            gross=gross, net=net, k_at_entry=pos.k_at_entry, d_at_entry=pos.d_at_entry,
            atr_at_entry=pos.atr_at_entry, bandwidth_at_entry=pos.bandwidth_at_entry,
            multiple_at_entry=pos.multiple_at_entry,
            fee_points_at_entry=pos.fee_points_at_entry, exit_reason=reason,
        )
        (res.trades_a if mode == "A" else res.trades_b).append(t)
        return net

    for i in range(n - 1):
        day = ts[i] // 86_400_000

        # ---------- 1. 灾难止损 (用本根的 high/low 触发) ----------
        if pos_a is not None:
            stop_dist = DISASTER_ATR * pos_a.atr_at_entry
            trig = None
            if pos_a.side == 1 and l[i] <= pos_a.entry_px - stop_dist:
                trig = pos_a.entry_px - stop_dist
            elif pos_a.side == -1 and h[i] >= pos_a.entry_px + stop_dist:
                trig = pos_a.entry_px + stop_dist
            if trig is not None:
                equity_a += close_pos(pos_a, ts[i], trig, "灾难止损", "A")
                pos_a = None
                peak_a = max(peak_a, equity_a)
        if pos_b is not None:
            stop_dist = DISASTER_ATR * pos_b.atr_at_entry
            trig = None
            if pos_b.side == 1 and l[i] <= pos_b.entry_px - stop_dist:
                trig = pos_b.entry_px - stop_dist
            elif pos_b.side == -1 and h[i] >= pos_b.entry_px + stop_dist:
                trig = pos_b.entry_px + stop_dist
            if trig is not None:
                equity_b += close_pos(pos_b, ts[i], trig, "灾难止损", "B")
                pos_b = None
                peak_b = max(peak_b, equity_b)

        # ---------- 2. 盯市权益 ----------
        mtm_a = equity_a + ((c[i] - pos_a.entry_px) * pos_a.qty * pos_a.side
                            if pos_a else 0.0)
        mtm_b = equity_b + ((c[i] - pos_b.entry_px) * pos_b.qty * pos_b.side
                            if pos_b else 0.0)
        peak_a = max(peak_a, mtm_a)
        peak_b = max(peak_b, mtm_b)

        # ---------- 3. 风控闸门 ----------
        if day_key_a != day:
            day_key_a, day_start_eq_a = day, mtm_a
        if day_key_b != day:
            day_key_b, day_start_eq_b = day, mtm_b
        daily_loss_a = (day_start_eq_a - mtm_a) / day_start_eq_a if day_start_eq_a else 0.0
        dd_a = (peak_a - mtm_a) / peak_a if peak_a else 0.0
        if dd_a >= MAX_DRAWDOWN and not halted_a:
            halted_a = True
            res.halts.append({"ts": int(ts[i]), "reason": "累计回撤 ≥ 10%",
                              "drawdown": dd_a, "equity": mtm_a})
        block_new_a = halted_a or daily_loss_a >= DAILY_LOSS_LIMIT

        daily_loss_b = (day_start_eq_b - mtm_b) / day_start_eq_b if day_start_eq_b else 0.0
        dd_b = (peak_b - mtm_b) / peak_b if peak_b else 0.0
        block_new_b = dd_b >= MAX_DRAWDOWN or daily_loss_b >= DAILY_LOSS_LIMIT

        # ---------- 4. 信号判定 (t 收盘) ----------
        gold = bool(k[i] > d[i] and k[i - 1] <= d[i - 1])
        dead = bool(k[i] < d[i] and k[i - 1] >= d[i - 1])
        sig_long = bool(gold and k[i] < K_LONG_MAX)
        sig_short = bool(dead and k[i] > K_SHORT_MIN)
        w = k[max(0, i - LOOSE_LOOKBACK + 1):i + 1]
        loose_long = bool(gold and float(np.min(w)) < K_LONG_MAX)
        loose_short = bool(dead and float(np.max(w)) > K_SHORT_MIN)

        # 布林闸门
        if np.isnan(up[i]) or np.isnan(lb[i]):
            bandwidth = target = mult = float("nan")
        else:
            bandwidth = float(up[i] - lb[i])
            target = bandwidth / 2.0
            fee_points = GATE_FEE_RATE * float(c[i])
            mult = target / fee_points if fee_points > 0 else 0.0
        if not BOLL_GATE_ENABLED:
            r_eff = RISK_R
        elif np.isnan(up[i]) or np.isnan(lb[i]):
            r_eff = None
        elif mult >= GATE_STRONG:
            r_eff = RISK_R
        elif mult >= GATE_WEAK:
            r_eff = RISK_R / 2.0
        else:
            r_eff = None

        # ---------- 5. 执行 (t+1 开盘) ----------
        px_next = float(o[i + 1])
        ms_next = int(ts[i + 1])

        # --- 模式 A ---
        if pos_a is None:
            want = 1 if sig_long else (-1 if sig_short else 0)
            if want != 0:
                reason = None
                if block_new_a:
                    reason = "风控闸门: 当日亏损或回撤超限"
                elif r_eff is None:
                    reason = "布林闸门: 倍数 < 2"
                elif np.isnan(atr1h_aligned[i]) or atr1h_aligned[i] <= 0:
                    reason = "ATR_1H 不可用"
                if reason:
                    res.skips.append({"ts": ms_next, "side": want, "reason": reason,
                                      "multiple": mult})
                else:
                    qty = floor_step(mtm_a * r_eff / (ATR_MULT_K * atr1h_aligned[i]))
                    if qty < MIN_QTY or qty * px_next < MIN_NOTIONAL:
                        res.skips.append({"ts": ms_next, "side": want,
                                          "reason": f"数量不足 (qty={qty:.4f}, "
                                                    f"名义={qty*px_next:.2f})",
                                          "multiple": mult})
                    else:
                        fee = px_next * qty * FEE_PER_SIDE
                        equity_a -= fee
                        pos_a = OpenPosition(
                            side=want, entry_ms=ms_next, entry_px=px_next, qty=qty,
                            fee=fee, k_at_entry=float(k[i]), d_at_entry=float(d[i]),
                            atr_at_entry=float(atr1h_aligned[i]),
                            bandwidth_at_entry=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
                            multiple_at_entry=float(mult) if not np.isnan(mult) else 0.0,
                            fee_points_at_entry=GATE_FEE_RATE * float(c[i]),
                        )
        else:
            opposite = (pos_a.side == 1 and sig_short) or (pos_a.side == -1 and sig_long)
            if opposite:
                equity_a += close_pos(pos_a, ms_next, px_next, "信号反转", "A")
                pos_a = None
                want = 1 if sig_long else -1
                if not block_new_a and r_eff is not None and not np.isnan(atr1h_aligned[i]):
                    qty = floor_step(mtm_a * r_eff / (ATR_MULT_K * atr1h_aligned[i]))
                    if qty >= MIN_QTY and qty * px_next >= MIN_NOTIONAL:
                        fee = px_next * qty * FEE_PER_SIDE
                        equity_a -= fee
                        pos_a = OpenPosition(
                            side=want, entry_ms=ms_next, entry_px=px_next, qty=qty,
                            fee=fee, k_at_entry=float(k[i]), d_at_entry=float(d[i]),
                            atr_at_entry=float(atr1h_aligned[i]),
                            bandwidth_at_entry=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
                            multiple_at_entry=float(mult) if not np.isnan(mult) else 0.0,
                            fee_points_at_entry=GATE_FEE_RATE * float(c[i]),
                        )

        # --- 模式 B (并行记录, 不执行): 对侧裸交叉即平仓 ---
        if pos_b is None:
            want = 1 if sig_long else (-1 if sig_short else 0)
            if want != 0 and not block_new_b and r_eff is not None \
                    and not np.isnan(atr1h_aligned[i]):
                qty = floor_step(mtm_b * r_eff / (ATR_MULT_K * atr1h_aligned[i]))
                if qty >= MIN_QTY and qty * px_next >= MIN_NOTIONAL:
                    fee = px_next * qty * FEE_PER_SIDE
                    equity_b -= fee
                    pos_b = OpenPosition(
                        side=want, entry_ms=ms_next, entry_px=px_next, qty=qty,
                        fee=fee, k_at_entry=float(k[i]), d_at_entry=float(d[i]),
                        atr_at_entry=float(atr1h_aligned[i]),
                        bandwidth_at_entry=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
                        multiple_at_entry=float(mult) if not np.isnan(mult) else 0.0,
                        fee_points_at_entry=GATE_FEE_RATE * float(c[i]),
                    )
        else:
            if (pos_b.side == 1 and dead) or (pos_b.side == -1 and gold):
                equity_b += close_pos(pos_b, ms_next, px_next, "对侧裸交叉", "B")
                pos_b = None

        # ---------- 6. 逐根日志 ----------
        res.bars.append(BarRow(
            ts=int(ts[i]), o=float(o[i]), h=float(h[i]), l=float(l[i]), c=float(c[i]),
            v=float(v[i]), k=float(k[i]), d=float(d[i]), j=float(j[i]),
            atr_1h=float(atr1h_aligned[i]), up=float(up[i]), mb=float(mb[i]),
            lb=float(lb[i]), bandwidth=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
            target_dist=float(target) if not np.isnan(target) else 0.0,
            multiple=float(mult) if not np.isnan(mult) else 0.0,
            gold_cross=gold, dead_cross=dead, sig_long=sig_long, sig_short=sig_short,
            loose_long=loose_long, loose_short=loose_short,
            pos_side=pos_a.side if pos_a else 0,
            pos_qty=pos_a.qty if pos_a else 0.0,
        ))

    res.final_equity_a = equity_a
    res.final_equity_b = equity_b
    res.peak_a = peak_a
    res.peak_b = peak_b
    return res
