"""KDJ(9,3,3) 信号的唯一来源：仅用已收盘的相邻两根 K/D 值。

15m 与 5m 同一套规则：当根收盘交叉，且 MACD 能量柱方向与交叉方向一致，
下一根开盘下限价单。交叉方向与能量柱背离 → 丢弃，不平仓、不反手。
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

# MACD 参数 (与图上一致)：DIF = EMA12 − EMA26，DEA = EMA9(DIF)，柱 = DIF − DEA。
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# 2026-10-02 用户新规则：15m 的方向过滤从「价格突破上一根高低点」换成
# 「MACD 能量柱正负」。绿柱(>0)只许做多，红柱(<0)只许做空，背离一律丢弃。
# BREAK_ATR_MULT 与 price_breaks() 保留兼容历史测试，已无规格使用。
BREAK_ATR_MULT = 0.15


def crossing(k_previous: float, d_previous: float,
             k_current: float, d_current: float) -> Tuple[bool, bool]:
    """返回 (金叉做多, 死叉做空)。NaN 不能生成交易信号。"""
    if not all(math.isfinite(float(x)) for x in
               (k_previous, d_previous, k_current, d_current)):
        return False, False
    gold = k_previous <= d_previous and k_current > d_current
    dead = k_previous >= d_previous and k_current < d_current
    return gold, dead


def entry_signal(k_previous: float, d_previous: float,
                 k_current: float, d_current: float,
                 *,
                 k_long_max: Optional[float] = None,
                 k_short_min: Optional[float] = None
                 ) -> Tuple[bool, bool, bool, bool]:
    """返回 (做多, 做空, 金叉, 死叉)。

    k_long_max / k_short_min 是已停用的旧 5m K 极值过滤：2026-10-03 起四条规格
    统一改用 macd_gate 的能量柱方向。参数与分支仅为历史测试/研究对比保留。
    """
    gold, dead = crossing(k_previous, d_previous, k_current, d_current)
    sig_long = bool(gold and (k_long_max is None or k_current < k_long_max))
    sig_short = bool(dead and (k_short_min is None or k_current > k_short_min))
    return sig_long, sig_short, gold, dead


def price_breaks(close: float, prev_high: float, prev_low: float, atr: float,
                 *, want: int) -> bool:
    """已停用的 15m 旧闸门：金叉要涨破上一根最高，死叉要跌破上一根最低。

    2026-10-02 起 15m 改用 macd_gate；本函数与 BREAK_ATR_MULT 只为兼容
    历史测试与报表保留，任何规格都不再调用它。
    """
    vals = (close, prev_high, prev_low, atr)
    if want == 0 or not all(math.isfinite(float(x)) for x in vals) or atr <= 0:
        return False
    pad = BREAK_ATR_MULT * float(atr)
    if want > 0:
        return float(close) > float(prev_high) + pad
    return float(close) < float(prev_low) - pad


def macd_side(hist) -> int:
    """能量柱方向：绿柱(>0) → +1，红柱(<0) → −1，0 或非有限值 → 0（不放行）。"""
    try:
        value = float(hist)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(value) or value == 0.0:
        return 0
    return 1 if value > 0 else -1


def macd_gate(sig_long: bool, sig_short: bool, hist, *,
              enabled: bool = True) -> Tuple[bool, bool, str]:
    """15m 方向闸门：交叉方向必须与 MACD 能量柱正负一致。

    返回 (做多, 做空, 丢弃说明)。背离时不产生任何动作 ——
    既不开新仓，也不平掉现有仓、更不反手。
    """
    if not enabled:
        return bool(sig_long), bool(sig_short), ""
    want = 1 if sig_long else (-1 if sig_short else 0)
    if want == 0:
        return False, False, ""
    side = macd_side(hist)
    if side == want:
        return bool(sig_long), bool(sig_short), ""
    if side == 0:
        return False, False, "MACD 能量柱为 0 或不可用，丢弃不操作"
    if want == 1:
        return False, False, "金叉但 MACD 是红柱，方向背离，丢弃不操作"
    return False, False, "死叉但 MACD 是绿柱，方向背离，丢弃不操作"


def confirmed_signal(
    k_prev2: float, d_prev2: float,
    k_prev: float, d_prev: float,
    k_cur: float, d_cur: float,
    *,
    k_long_max: Optional[float] = None,
    k_short_min: Optional[float] = None,
) -> Tuple[bool, bool, bool, bool, str]:
    """15m：上一根交叉，本根 K 仍在交叉一侧，才在本根下限价单。

    返回 (做多, 做空, 本根金叉, 本根死叉, 备注)。
    本根金叉/死叉只用于展示；开仓看的是上一根交叉是否站稳。
    """
    prev_long, prev_short, prev_gold, prev_dead = entry_signal(
        k_prev2, d_prev2, k_prev, d_prev,
        k_long_max=k_long_max, k_short_min=k_short_min,
    )
    gold, dead = crossing(k_prev, d_prev, k_cur, d_cur)
    if not all(math.isfinite(float(x)) for x in (k_cur, d_cur)):
        return False, False, gold, dead, "观察"
    hold_long = float(k_cur) > float(d_cur)
    hold_short = float(k_cur) < float(d_cur)
    sig_long = bool(prev_long and hold_long)
    sig_short = bool(prev_short and hold_short)
    if sig_long:
        note = "上一根金叉已确认，本根下限价单"
    elif sig_short:
        note = "上一根死叉已确认，本根下限价单"
    elif prev_gold or prev_dead:
        note = "上一根交叉未站稳，放弃"
    elif gold or dead:
        note = "交叉待确认，下一根再下单"
    else:
        note = "观察"
    return sig_long, sig_short, gold, dead, note
