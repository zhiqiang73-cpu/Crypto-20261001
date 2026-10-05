"""仅供离线 15m 验证的影子引擎副本；绝不可导入线上运行器。

复制自冻结的 shadow/engine.py。原引擎与线上文件均保持原样；新增可选
预热、费用/滑点、期末强制结算及逐根权益诊断，默认参数保留历史回放口径。

规格要点 (全部硬编码为常量, 不对外暴露为可调项):
    信号周期由 IntervalSpec 指定（15m / 5m），风险参数周期恒为 1H；只用已收盘 K 线。
    做多 = 金叉且 MACD 能量柱为正；做空 = 死叉且能量柱为负。
    方向背离的交叉丢弃（不平仓、不反手）；不要求 K 值进入极值区。
    出场 A: 对侧交叉平仓并反手；B: 对侧交叉只平仓。
    仓位: qty = 权益 × RISK_R ÷ (2 × ATR_1H), 向下取整到 stepSize。
    布林带仍计算并记日志，但不再过滤开仓。
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
from shadow.indicators import atr_wilder, boll, kdj, macd
from shadow.signals import crossing, entry_signal, macd_gate

# --------------------------------------------------------------------------- 规格常量 (禁止修改)
KDJ_N, KDJ_M1, KDJ_M2 = 9, 3, 3
ATR_PERIOD = 14
BOLL_N, BOLL_K = 20, 2.0
# 2026-10-02 用户新增：15m 的方向过滤 = MACD(12,26,9) 能量柱正负。
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MACD_GATE_ENABLED = True

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


@dataclass(frozen=True)
class IntervalSpec:
    """一次回放使用的周期规格；15m 与 5m 共用同一套引擎。

    entry_mode:
        "macd_gate"   —— 15m 与 5m：KD 交叉 + MACD 能量柱同向，背离丢弃
        "k_threshold" —— 仅保留兼容：金叉且 K<k_long_max 做多；死叉且 K>k_short_min 做空
    stop_atr_mult:
        正常止损距离（× ATR_1H）。None = 不设正常止损（历史行为）。
    break_even_trigger_atr_mult:
        浮盈达到该倍数（× 开仓时 ATR_1H）后，将价格止损移至入场价。
        同根 OHLC 无法确定先后顺序，故仅从下一根 K 线起生效；None = 不启用。
    disaster_atr_mult:
        灾难止损距离（× ATR_1H），始终生效，作为跳空/插针/断网的兜底。
    """

    name: str
    interval_ms: int
    entry_mode: str = "macd_gate"
    k_long_max: Optional[float] = None
    k_short_min: Optional[float] = None
    stop_atr_mult: Optional[float] = None
    break_even_trigger_atr_mult: Optional[float] = None
    disaster_atr_mult: float = DISASTER_ATR
    # False = 不因累计回撤锁死（仅供标定/研究使用，生产规格保持 True）
    halt_on_drawdown: bool = True
    # None = 跟随模块级 MACD_GATE_ENABLED（保留整体关闸做对比的能力）
    macd_gate_enabled: Optional[bool] = None


# 2026-10-03 研究定案：1.5×ATR_1H 止损 + 浮盈 1.5×ATR 后下根起保本。
SPEC_15M = IntervalSpec(
    name="15m", interval_ms=15 * 60 * 1000,
    stop_atr_mult=1.5, break_even_trigger_atr_mult=1.5,
)

# 2026-10-03：5m 回放规格与线上 5m 规格对齐 —— 入场 = KD 交叉且 MACD 能量柱同向，
# 不使用 K 极值过滤。背离丢弃。
SPEC_5M = IntervalSpec(
    name="5m", interval_ms=5 * 60 * 1000, entry_mode="macd_gate",
    k_long_max=None, k_short_min=None,
    # None = 跟随模块级 MACD_GATE_ENABLED，与 15m 回放同一套闸门开关。
    macd_gate_enabled=None,
    # 仅影子研究规格：浮盈 1.5×ATR 后，下根 K 线起移至开仓价。
    break_even_trigger_atr_mult=1.5,
)


def floor_step(qty: float) -> float:
    """按交易所最小变动单位向下取整。"""
    if qty <= 0:
        return 0.0
    return math.floor(qty / STEP_SIZE) * STEP_SIZE


@dataclass
class ShadowConfig:
    equity0: float = 1000.0
    # 研究用：每次开仓都按这一固定权益计算风险金额，切断复利路径。
    # None 保持历史的按实时盯市权益复利仓位逻辑。
    fixed_risk_equity: Optional[float] = None
    # 研究用：在固定风险基础上缩放数量（1.0 / 0.5 / 0.25 等）。
    position_scale: float = 1.0
    # 研究用：解除日亏与累计回撤对“是否继续取样”的机械截断；
    # 默认 False，15m 与既有生产回放行为不变。
    research_ignore_risk_gates: bool = False
    # 批量标定无需逐根输出时可关闭，避免为每个参数组合保留数十万条日志。
    # 默认 True，既有报告和 run_shadow 行为不变。
    record_bars: bool = True
    # 以下参数只存在于隔离研究副本；生产策略与原 engine.py 不读取它们。
    trade_start_ms: Optional[int] = None
    fee_per_side: float = FEE_PER_SIDE
    slippage_bps: float = 0.0
    gap_aware_stop: bool = False
    close_at_end: bool = False
    record_equity: bool = False


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
    # 价格达到保本触发位后置 True；止损从下一根 K 线开始生效。
    break_even_armed: bool = False


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
    macd_hist: float
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
    equity_curve_a: List[tuple] = field(default_factory=list)
    equity_curve_b: List[tuple] = field(default_factory=list)
    final_equity_a: float = 0.0
    final_equity_b: float = 0.0
    peak_a: float = 0.0
    peak_b: float = 0.0


def align_atr_1h(bars_ms: np.ndarray, bars1h_ms: np.ndarray,
                 atr1h: np.ndarray,
                 interval_ms: int = 15 * 60 * 1000) -> np.ndarray:
    """把 1H 的 ATR 对齐到每根信号 K 线: 取【已收盘】的最后一根 1H。

    信号 K 线 t 收盘于 bars_ms[t] + interval_ms; 1H 收盘于 bars1h_ms[j] + 60min。
    使用 bars1h_ms[j] + 60min ≤ bars_ms[t] + interval_ms 的最后一根。

    interval_ms 默认 15 分钟（保持既有调用行为），5m 回放传 5 分钟。
    """
    close_sig = bars_ms + interval_ms
    close1h = bars1h_ms + 60 * 60 * 1000
    idx = np.searchsorted(close1h, close_sig, side="right") - 1
    out = np.full(len(bars_ms), np.nan, dtype=np.float64)
    ok = idx >= 0
    out[ok] = atr1h[idx[ok]]
    return out


def _stop_hit(pos: OpenPosition, high: float, low: float,
              spec: IntervalSpec) -> Optional[tuple]:
    """本根 K 线是否触发止损。返回 (触发价, 原因)；未触发返回 None。

    已激活的保本止损位于入场价，优先于亏损方向的正常/灾难止损。
    正常止损（spec.stop_atr_mult）距离更近，同一根内必然先被穿过，
    因此再判正常止损、最后判灾难止损；三者都不设则永不触发。
    """
    levels = []
    if pos.break_even_armed:
        levels.append((0.0, "保本止损"))
    if spec.stop_atr_mult:
        levels.append((float(spec.stop_atr_mult), "止损"))
    if spec.disaster_atr_mult:
        levels.append((float(spec.disaster_atr_mult), "灾难止损"))
    for mult, why in levels:
        dist = mult * pos.atr_at_entry
        # 保本止损的距离恰为 0；正常/灾难止损不会把 0 加入 levels。
        if not math.isfinite(dist) or dist < 0:
            continue
        if pos.side == 1 and low <= pos.entry_px - dist:
            return pos.entry_px - dist, why
        if pos.side == -1 and high >= pos.entry_px + dist:
            return pos.entry_px + dist, why
    return None


def _arm_break_even(pos: OpenPosition, high: float, low: float,
                    spec: IntervalSpec) -> bool:
    """在本根达到浮盈阈值后为下一根 K 线激活价格保本止损。

    单根 OHLC 不提供高低点的先后顺序。为避免把同根内“先冲高、后回落”
    误写成确定可成交的保本退出，激活只作用于后续 K 线。
    """
    if pos.break_even_armed or not spec.break_even_trigger_atr_mult:
        return False
    dist = float(spec.break_even_trigger_atr_mult) * pos.atr_at_entry
    if not math.isfinite(dist) or dist <= 0:
        return False
    reached = ((pos.side == 1 and high >= pos.entry_px + dist)
               or (pos.side == -1 and low <= pos.entry_px - dist))
    if reached:
        pos.break_even_armed = True
    return reached


def run_shadow_spec(bars: Dict[str, np.ndarray], bars1h: Dict[str, np.ndarray],
                    spec: Optional[IntervalSpec] = None,
                    cfg: Optional[ShadowConfig] = None) -> ShadowResult:
    """按 spec 回放任意周期的已收盘 K 线（15m 与 5m 共用同一套逻辑）。"""
    spec = spec or SPEC_15M
    cfg = cfg or ShadowConfig()
    if cfg.fixed_risk_equity is not None and (
            not math.isfinite(cfg.fixed_risk_equity) or cfg.fixed_risk_equity <= 0):
        raise ValueError("fixed_risk_equity 必须是正的有限数")
    if not math.isfinite(cfg.position_scale) or cfg.position_scale <= 0:
        raise ValueError("position_scale 必须是正的有限数")
    if not math.isfinite(cfg.fee_per_side) or not 0 <= cfg.fee_per_side <= 0.01:
        raise ValueError("fee_per_side 超出离线研究范围")
    if not math.isfinite(cfg.slippage_bps) or not 0 <= cfg.slippage_bps <= 500:
        raise ValueError("slippage_bps 超出离线研究范围")
    n = len(bars["close"])
    if n < 2:
        raise ValueError("至少需要两根已收盘 K 线")
    o, h, l, c, v = (bars[x] for x in ("open", "high", "low", "close", "volume"))
    ts = bars["ts"]

    k, d, j = kdj(h, l, c, KDJ_N, KDJ_M1, KDJ_M2)
    _dif, _dea, hist = macd(c, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    mb, up, lb, _sd = boll(c, BOLL_N, BOLL_K)
    atr1h_aligned = align_atr_1h(ts, bars1h["ts"],
                                 atr_wilder(bars1h["high"], bars1h["low"],
                                            bars1h["close"], ATR_PERIOD),
                                 spec.interval_ms)

    res = ShadowResult()

    def sizing_equity(mtm: float) -> float:
        """返回仓位公式使用的权益；研究模式可固定风险预算而不复利。"""
        return (float(cfg.fixed_risk_equity)
                if cfg.fixed_risk_equity is not None else mtm)

    def sized_qty(mtm: float, r_eff: float, atr: float) -> float:
        raw = sizing_equity(mtm) * r_eff / (ATR_MULT_K * atr)
        return floor_step(raw * cfg.position_scale)

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
    halted_b = False

    def close_pos(pos: OpenPosition, ms: int, px: float, reason: str,
                  mode: str, stop_open: Optional[float] = None) -> float:
        if stop_open is not None and cfg.gap_aware_stop:
            px = min(px, stop_open) if pos.side == 1 else max(px, stop_open)
        # 多仓退出卖得更低，空仓退出买得更高。
        px *= 1.0 - pos.side * cfg.slippage_bps / 10_000.0
        gross = (px - pos.entry_px) * pos.qty * pos.side
        exit_fee = px * pos.qty * cfg.fee_per_side
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
        # 返回的是**现金变动**：入场费已在开仓时从权益里扣掉，这里只能补
        # 「毛盈亏 − 出场费」。若返回 net（已含入场费）会把入场费扣两次，
        # 导致权益、收益率与最大回撤系统性偏悲观。Trade.net 仍保留完整口径。
        return gross - exit_fee

    for i in range(n - 1):
        day = ts[i] // 86_400_000
        stopped_a_this_bar = False
        stopped_b_this_bar = False

        # ---------- 1. 价格止损 (用本根的 high/low 触发) ----------
        if pos_a is not None:
            hit = _stop_hit(pos_a, float(h[i]), float(l[i]), spec)
            if hit is not None:
                equity_a += close_pos(pos_a, ts[i], hit[0], hit[1], "A",
                                      stop_open=float(o[i]))
                pos_a = None
                peak_a = max(peak_a, equity_a)
                stopped_a_this_bar = True
            elif pos_a is not None:
                _arm_break_even(pos_a, float(h[i]), float(l[i]), spec)
        if pos_b is not None:
            hit = _stop_hit(pos_b, float(h[i]), float(l[i]), spec)
            if hit is not None:
                equity_b += close_pos(pos_b, ts[i], hit[0], hit[1], "B",
                                      stop_open=float(o[i]))
                pos_b = None
                peak_b = max(peak_b, equity_b)
                stopped_b_this_bar = True
            elif pos_b is not None:
                _arm_break_even(pos_b, float(h[i]), float(l[i]), spec)

        # ---------- 2. 盯市权益 ----------
        mtm_a = equity_a + ((c[i] - pos_a.entry_px) * pos_a.qty * pos_a.side
                            if pos_a else 0.0)
        mtm_b = equity_b + ((c[i] - pos_b.entry_px) * pos_b.qty * pos_b.side
                            if pos_b else 0.0)
        peak_a = max(peak_a, mtm_a)
        peak_b = max(peak_b, mtm_b)
        if cfg.record_equity and (cfg.trade_start_ms is None or
                                  int(ts[i]) + spec.interval_ms >= cfg.trade_start_ms):
            res.equity_curve_a.append((int(ts[i]) + spec.interval_ms, mtm_a))
            res.equity_curve_b.append((int(ts[i]) + spec.interval_ms, mtm_b))

        # ---------- 3. 风控闸门 ----------
        if day_key_a != day:
            day_key_a, day_start_eq_a = day, mtm_a
        if day_key_b != day:
            day_key_b, day_start_eq_b = day, mtm_b
        daily_loss_a = (day_start_eq_a - mtm_a) / day_start_eq_a if day_start_eq_a else 0.0
        dd_a = (peak_a - mtm_a) / peak_a if peak_a else 0.0
        if (not cfg.research_ignore_risk_gates and spec.halt_on_drawdown
                and dd_a >= MAX_DRAWDOWN and not halted_a):
            halted_a = True
            res.halts.append({"ts": int(ts[i]), "reason": "累计回撤 ≥ 10%",
                              "drawdown": dd_a, "equity": mtm_a})
        block_new_a = (False if cfg.research_ignore_risk_gates
                       else halted_a or daily_loss_a >= DAILY_LOSS_LIMIT)

        daily_loss_b = (day_start_eq_b - mtm_b) / day_start_eq_b if day_start_eq_b else 0.0
        dd_b = (peak_b - mtm_b) / peak_b if peak_b else 0.0
        if (not cfg.research_ignore_risk_gates and spec.halt_on_drawdown
                and dd_b >= MAX_DRAWDOWN and not halted_b):
            halted_b = True
            res.halts.append({"ts": int(ts[i]), "reason": "B 累计回撤 ≥ 10%",
                              "drawdown": dd_b, "equity": mtm_b, "mode": "B"})
        block_new_b = (False if cfg.research_ignore_risk_gates else
                       halted_b or daily_loss_b >= DAILY_LOSS_LIMIT)

        # ---------- 4. 信号判定 (t 收盘) ----------
        if spec.entry_mode == "k_threshold":
            # 旧 5m 分支，已无规格使用：交叉 + K 极值。2026-10-03 起四条规格
            # 都走下面的 macd_gate；entry_mode="k_threshold" 仅留作研究对比。
            sig_long, sig_short, gold, dead = entry_signal(
                k[i - 1], d[i - 1], k[i], d[i],
                k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
            )
        else:
            # 第一根没有历史 K/D；Python 的 [-1] 会偷看整个窗口的最后一根。
            gold, dead = (crossing(k[i - 1], d[i - 1], k[i], d[i])
                          if i > 0 else (False, False))
            gate_on = (MACD_GATE_ENABLED if spec.macd_gate_enabled is None
                       else bool(spec.macd_gate_enabled))
            sig_long, sig_short, _macd_note = macd_gate(
                gold, dead, float(hist[i]), enabled=gate_on,
            )
        # 「宽松」列 = 未经方向闸门的裸交叉, 保留用于对比闸门挡掉了多少。
        loose_long, loose_short = gold, dead

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
        if cfg.trade_start_ms is not None and ms_next < cfg.trade_start_ms:
            # 允许 KDJ/MACD/ATR 预热，不允许预热段交易污染窗口权益。
            continue

        # --- 模式 A ---
        if pos_a is None:
            want = 1 if sig_long else (-1 if sig_short else 0)
            if want != 0:
                reason = "止损后等待下一完整信号" if stopped_a_this_bar else None
                if reason is None and block_new_a:
                    reason = "风控闸门: 当日亏损或回撤超限"
                elif reason is None and r_eff is None:
                    reason = "布林闸门: 倍数 < 2"
                elif reason is None and (np.isnan(atr1h_aligned[i])
                                         or atr1h_aligned[i] <= 0):
                    reason = "ATR_1H 不可用"
                if reason:
                    res.skips.append({"ts": ms_next, "side": want, "reason": reason,
                                      "multiple": mult})
                else:
                    qty = sized_qty(mtm_a, r_eff, atr1h_aligned[i])
                    if qty < MIN_QTY or qty * px_next < MIN_NOTIONAL:
                        res.skips.append({"ts": ms_next, "side": want,
                                          "reason": f"数量不足 (qty={qty:.4f}, "
                                                    f"名义={qty*px_next:.2f})",
                                          "multiple": mult})
                    else:
                        entry_fill = px_next * (1.0 + want * cfg.slippage_bps / 10_000.0)
                        fee = entry_fill * qty * cfg.fee_per_side
                        equity_a -= fee
                        pos_a = OpenPosition(
                            side=want, entry_ms=ms_next, entry_px=entry_fill, qty=qty,
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
                    qty = sized_qty(mtm_a, r_eff, atr1h_aligned[i])
                    if qty >= MIN_QTY and qty * px_next >= MIN_NOTIONAL:
                        entry_fill = px_next * (1.0 + want * cfg.slippage_bps / 10_000.0)
                        fee = entry_fill * qty * cfg.fee_per_side
                        equity_a -= fee
                        pos_a = OpenPosition(
                            side=want, entry_ms=ms_next, entry_px=entry_fill, qty=qty,
                            fee=fee, k_at_entry=float(k[i]), d_at_entry=float(d[i]),
                            atr_at_entry=float(atr1h_aligned[i]),
                            bandwidth_at_entry=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
                            multiple_at_entry=float(mult) if not np.isnan(mult) else 0.0,
                            fee_points_at_entry=GATE_FEE_RATE * float(c[i]),
                        )

        # --- 模式 B (并行记录, 不执行): 对侧裸交叉即平仓 ---
        if pos_b is None:
            want = 1 if sig_long else (-1 if sig_short else 0)
            if want != 0 and not stopped_b_this_bar and not block_new_b and r_eff is not None \
                    and not np.isnan(atr1h_aligned[i]):
                qty = sized_qty(mtm_b, r_eff, atr1h_aligned[i])
                if qty >= MIN_QTY and qty * px_next >= MIN_NOTIONAL:
                    entry_fill = px_next * (1.0 + want * cfg.slippage_bps / 10_000.0)
                    fee = entry_fill * qty * cfg.fee_per_side
                    equity_b -= fee
                    pos_b = OpenPosition(
                        side=want, entry_ms=ms_next, entry_px=entry_fill, qty=qty,
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
        if cfg.record_bars:
            res.bars.append(BarRow(
                ts=int(ts[i]), o=float(o[i]), h=float(h[i]), l=float(l[i]), c=float(c[i]),
                v=float(v[i]), k=float(k[i]), d=float(d[i]), j=float(j[i]),
                atr_1h=float(atr1h_aligned[i]), up=float(up[i]), mb=float(mb[i]),
                lb=float(lb[i]), bandwidth=float(bandwidth) if not np.isnan(bandwidth) else 0.0,
                target_dist=float(target) if not np.isnan(target) else 0.0,
                multiple=float(mult) if not np.isnan(mult) else 0.0,
                macd_hist=float(hist[i]),
                gold_cross=gold, dead_cross=dead, sig_long=sig_long, sig_short=sig_short,
                loose_long=loose_long, loose_short=loose_short,
                pos_side=pos_a.side if pos_a else 0,
                pos_qty=pos_a.qty if pos_a else 0.0,
            ))

    if cfg.close_at_end:
        terminal_ms = int(ts[-1]) + spec.interval_ms
        terminal_px = float(c[-1])
        if pos_a is not None:
            equity_a += close_pos(pos_a, terminal_ms, terminal_px,
                                  "窗口末强制结算", "A")
        if pos_b is not None:
            equity_b += close_pos(pos_b, terminal_ms, terminal_px,
                                  "窗口末强制结算", "B")
        if cfg.record_equity:
            res.equity_curve_a.append((terminal_ms, equity_a))
            res.equity_curve_b.append((terminal_ms, equity_b))
        peak_a = max(peak_a, equity_a)
        peak_b = max(peak_b, equity_b)
    res.final_equity_a = equity_a
    res.final_equity_b = equity_b
    res.peak_a = peak_a
    res.peak_b = peak_b
    return res


def run_shadow(bars15: Dict[str, np.ndarray], bars1h: Dict[str, np.ndarray],
               cfg: Optional[ShadowConfig] = None) -> ShadowResult:
    """15m 回放：保持原签名与行为（shadow/run.py、shadow/__init__.py 与测试依赖它）。"""
    return run_shadow_spec(bars15, bars1h, SPEC_15M, cfg)
