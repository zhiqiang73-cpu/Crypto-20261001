"""V8.1 三层出场检查器: ATR 倍数优先, 固定 % 回退.

优先级:
  1. 硬止损 / 市场异常 → 全平
  2. TP1 / TP2
  3. 追踪止盈
  4. CS 衰减减仓
  5. 时间止损
  6. Predict.fun 突变 → 收紧追踪
"""

from __future__ import annotations

import time
from typing import List, Optional

from config.review import EXIT_STRATEGY
from trading.models import ExitAction, HorizonPosition


def _level_pct(
    cfg: dict,
    atr_key: str,
    pct_key: str,
    atr: float,
    entry_price: float,
    default_pct: float,
) -> float:
    """ATR 倍数 → 价格百分比; 无 ATR 时回退固定 pct."""
    atr_mult = cfg.get(atr_key)
    if atr is not None and atr > 0 and entry_price > 0 and atr_mult is not None:
        return float(atr_mult) * atr / entry_price
    return float(cfg.get(pct_key) or default_pct)


class ExitChecker:
    def check_exits(
        self,
        position: HorizonPosition,
        mark_price: float,
        cs: float,
        *,
        now_ms: Optional[int] = None,
        spread_vs_mean: Optional[float] = None,
        liq_5m_usd: Optional[float] = None,
        predict_fun_up_prob: Optional[float] = None,
        prev_predict_fun_up_prob: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> List[ExitAction]:
        if position is None or position.is_flat():
            return []
        if mark_price <= 0 or position.entry_price <= 0:
            return []

        cfg = EXIT_STRATEGY.get(position.horizon) or EXIT_STRATEGY["short_term"]
        now_ms = now_ms or int(time.time() * 1000)
        pnl = position.pnl_pct(mark_price)
        # 入场 ATR 优先 (仓位与止损一致); 否则用当前 ATR
        use_atr = float(position.entry_atr or 0) or float(atr or 0) or 0.0

        self._update_peak_and_trailing(position, mark_price, cfg, use_atr)

        # ---- 1. 市场异常 / 硬止损 ----
        spread_thr = float(cfg.get("spread_force_mult") or 3.0)
        if spread_vs_mean is not None and spread_vs_mean >= spread_thr:
            return [ExitAction(kind="full_close", close_pct=1.0, reason="spread_force")]

        liq_thr = float(cfg.get("liq_force_usd") or 100_000_000)
        if liq_5m_usd is not None and liq_5m_usd >= liq_thr:
            return [ExitAction(kind="full_close", close_pct=1.0, reason="liq_force")]

        hard_sl = _level_pct(
            cfg, "hard_sl_atr", "hard_sl_pct", use_atr, position.entry_price, 0.01
        )
        if pnl <= -hard_sl:
            return [ExitAction(kind="full_close", close_pct=1.0, reason="hard_sl")]

        # ---- 2. 分阶段止盈 ----
        tp1 = _level_pct(
            cfg, "tp1_atr", "tp1_pct", use_atr, position.entry_price, 0.008
        )
        tp1_close = float(cfg.get("tp1_close_pct") or 0.50)
        tp2 = _level_pct(
            cfg, "tp2_atr", "tp2_pct", use_atr, position.entry_price, 0.015
        )
        tp2_close = float(cfg.get("tp2_close_pct") or 0.30)

        if position.tp_levels_hit < 1 and pnl >= tp1:
            return [ExitAction(
                kind="partial_close", close_pct=tp1_close, reason="tp1",
            )]

        if position.tp_levels_hit < 2 and pnl >= tp2:
            return [ExitAction(
                kind="partial_close", close_pct=tp2_close, reason="tp2",
            )]

        # ---- 3. 追踪止盈 ----
        trail = _level_pct(
            cfg, "trailing_atr", "trailing_pct", use_atr, position.entry_price, 0.005
        )
        if position.trailing_tightened:
            trail = _level_pct(
                cfg,
                "tighten_trailing_atr",
                "tighten_trailing_pct",
                use_atr,
                position.entry_price,
                trail,
            )
        if position.tp_levels_hit >= 1 or pnl >= tp1 * 0.8:
            if position.peak_price > 0:
                if position.side == "LONG":
                    drawdown = (position.peak_price - mark_price) / position.peak_price
                else:
                    drawdown = (mark_price - position.peak_price) / position.peak_price
                if drawdown >= trail:
                    return [ExitAction(
                        kind="full_close", close_pct=1.0, reason="trailing_stop",
                    )]

        # ---- 4. CS 衰减 ----
        decay_thr = float(cfg.get("cs_decay_threshold") or 25)
        decay_close = float(cfg.get("cs_decay_close_pct") or 0.50)
        entry_cs = float(position.entry_cs or 0.0)
        if (
            not position.cs_decay_done
            and abs(entry_cs) > 10
            and abs(entry_cs) - abs(cs) >= decay_thr
            and (entry_cs * cs > 0 or abs(cs) < abs(entry_cs) * 0.3)
        ):
            return [ExitAction(
                kind="partial_close", close_pct=decay_close, reason="cs_decay",
            )]

        # ---- 5. 时间止损 ----
        time_stop_min = cfg.get("time_stop_min")
        min_pnl = cfg.get("time_stop_min_pnl")
        if time_stop_min is not None and position.opened_at_ms > 0:
            held_min = (now_ms - position.opened_at_ms) / 60_000.0
            if held_min >= float(time_stop_min) and pnl < float(min_pnl or 0.003):
                return [ExitAction(
                    kind="full_close", close_pct=1.0, reason="time_stop",
                )]

        # ---- 6. Predict.fun 突变 ----
        if (
            predict_fun_up_prob is not None
            and prev_predict_fun_up_prob is not None
            and not position.trailing_tightened
        ):
            flipped = (
                (prev_predict_fun_up_prob > 0.60 and predict_fun_up_prob < 0.40)
                or (prev_predict_fun_up_prob < 0.40 and predict_fun_up_prob > 0.60)
            )
            against = (
                (position.side == "LONG" and predict_fun_up_prob < 0.40)
                or (position.side == "SHORT" and predict_fun_up_prob > 0.60)
            )
            if flipped and against:
                return [ExitAction(
                    kind="tighten_trailing",
                    close_pct=0.0,
                    reason="predict_fun_flip",
                    new_trailing_pct=_level_pct(
                        cfg,
                        "tighten_trailing_atr",
                        "tighten_trailing_pct",
                        use_atr,
                        position.entry_price,
                        0.003,
                    ),
                )]

        return []

    @staticmethod
    def _update_peak_and_trailing(
        position: HorizonPosition,
        mark_price: float,
        cfg: dict,
        atr: float,
    ) -> None:
        if position.peak_price <= 0:
            position.peak_price = mark_price
        elif position.side == "LONG":
            position.peak_price = max(position.peak_price, mark_price)
        else:
            position.peak_price = min(position.peak_price, mark_price)

        trail = _level_pct(
            cfg, "trailing_atr", "trailing_pct", atr, position.entry_price, 0.005
        )
        if position.trailing_tightened:
            trail = _level_pct(
                cfg,
                "tighten_trailing_atr",
                "tighten_trailing_pct",
                atr,
                position.entry_price,
                trail,
            )
        if position.side == "LONG":
            position.trailing_stop_price = position.peak_price * (1.0 - trail)
        else:
            position.trailing_stop_price = position.peak_price * (1.0 + trail)
