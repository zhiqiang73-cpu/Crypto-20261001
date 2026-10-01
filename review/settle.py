"""按「事后价格」结算一笔交易的对错.

判定规则 (对称, 不预设多空偏向):
    入场后进入观察窗口, 沿 K 线逐根前进, 先触及哪一边就算哪一边:
      * 先触及 entry ± target_atr_mult × ATR  → CORRECT
      * 先触及 entry ∓ stop_atr_mult   × ATR  → WRONG
      * 窗口走完两边都没触及 (横盘)            → INVALID  (不计入分母)
      * 同一根 K 线内两边都触及                → 保守记 WRONG (标注 ambiguous)
      * 窗口还没走完                          → PENDING
      * 观望/中性 (没开仓) / 被黑天鹅覆盖      → EXCLUDED (不计入分母)

分母是「有效样本」(CORRECT + WRONG)。横盘、没开仓与被覆盖的笔不进胜率 ——
否则参数微调会被一堆「什么都没发生」的笔稀释成噪声。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.review import (
    ACTIONABLE_DECISIONS,
    AMBIGUOUS_COUNTS_AS,
    ATR_FALLBACK_PCT,
    SETTLE_CONFIG,
)
from indicators.classic import atr as atr_series
from indicators.ohlcv import Candle
from models.review import SettleStatus, TradeRecord, now_ms as _now_ms

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000


# --------------------------------------------------------------------------- 工具
def interval_hours(interval: str) -> float:
    """'15m' → 0.25, '4h' → 4.0, '1d' → 24.0."""
    unit = interval[-1].lower()
    try:
        value = float(interval[:-1])
    except ValueError:
        return 1.0
    return {"m": value / 60.0, "h": value, "d": value * 24.0}.get(unit, 1.0)


def plan_levels(record: TradeRecord, cfg: Optional[Dict[str, Any]] = None
                ) -> Tuple[str, float, float, float]:
    """返回 (方向, 目标价, 止损价, 实际使用的 ATR).

    方向以 record.decision 为准 (那才是系统真正据以开仓的结论), 而不是 CS 的符号 ——
    两者若不一致 (例如人工补录时填错), 以决策为准更不容易判反。
    """
    cfg = cfg or SETTLE_CONFIG[record.horizon]
    direction = record.direction
    if record.decision in ACTIONABLE_DECISIONS:
        direction = "LONG" if record.decision.endswith("LONG") else "SHORT"
    sign = 1.0 if direction == "LONG" else -1.0

    atr_used = record.atr
    if not atr_used or atr_used <= 0:
        atr_used = record.entry_price * ATR_FALLBACK_PCT

    target = record.entry_price + sign * cfg["target_atr_mult"] * atr_used
    stop = record.entry_price - sign * cfg["stop_atr_mult"] * atr_used
    return direction, target, stop, atr_used


def _atr_from_candles(candles: Sequence[Candle], period: int) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    series = atr_series(list(candles), period)
    for v in reversed(series):
        if v:
            return float(v)
    return None


# --------------------------------------------------------------------------- 结算
def settle_record(
    record: TradeRecord,
    candles: Sequence[Candle],
    now: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> TradeRecord:
    """就地结算一条档案并返回它 (调用方负责写回 journal)."""
    now = now if now is not None else _now_ms()
    horizon_cfg = cfg or SETTLE_CONFIG[record.horizon]
    window_ms = int(horizon_cfg["window_hours"] * HOUR_MS)
    window_end = record.opened_at_ms + window_ms

    # --- 不进分母的两类: 没开仓 (观望/中性) / 被黑天鹅覆盖 ---
    if record.overridden:
        record.status = SettleStatus.EXCLUDED.value
        record.settle_detail = {"reason": "overridden"}
        record.settled_at_ms = now
        return record

    if record.decision not in ACTIONABLE_DECISIONS:
        # 偏多/偏空观望与中性都没开仓, 事后价格再准也不构成「一笔成交」
        record.status = SettleStatus.EXCLUDED.value
        record.settle_detail = {
            "reason": "neutral_no_trade" if record.direction == "NEUTRAL"
                      else "watch_no_position",
            "decision": record.decision,
        }
        record.settled_at_ms = now
        return record

    direction, target, stop, atr_used = plan_levels(record, horizon_cfg)

    # 严格时间截断: 只使用 close_time <= window_end 的完整 bar
    # 跨越 window_end 的 bar 不得用于确定性 CORRECT/WRONG（会引入窗外价格）
    bar_ms = int(interval_hours(horizon_cfg.get("interval", "1h")) * HOUR_MS)
    truncated = False
    cleaned = []
    for c in candles:
        if c.open_time_ms < record.opened_at_ms:
            # 入场落在 bar 中间 — 整根 bar 的 high/low 含入场前，标不确定
            close_t = c.close_time_ms or (c.open_time_ms + bar_ms - 1)
            if c.open_time_ms <= record.opened_at_ms < close_t:
                truncated = True  # 入场 bar 不可可靠使用
            continue
        close_t = c.close_time_ms or (c.open_time_ms + bar_ms - 1)
        if c.open_time_ms >= window_end:
            continue
        if close_t > window_end:
            # bar 延伸到窗外 — 丢弃，不得用其 high/low 下确定性结论
            truncated = True
            continue
        cleaned.append(c)
    window = cleaned

    if not window:
        oldest = min((c.open_time_ms for c in candles), default=None)
        if truncated:
            record.status = SettleStatus.INVALID.value
            record.settle_detail = {
                "reason": "data_insufficient",
                "uncertain": True,
                "window_truncated": True,
                "note": "仅有跨越窗口边界或入场 bar 的 K 线，无法无前视地判定",
            }
            record.settled_at_ms = now
        elif oldest is not None and oldest >= window_end:
            record.status = SettleStatus.INVALID.value
            record.settle_detail = {"reason": "out_of_range",
                                    "note": "观察窗口早于可取 K 线范围, 无法结算"}
            record.settled_at_ms = now
        else:
            record.status = SettleStatus.PENDING.value
            record.settle_detail = {"reason": "window_not_elapsed"}
        return record

    mfe = 0.0   # 最有利 (ATR 倍数)
    mae = 0.0   # 最不利 (ATR 倍数)
    hit: Optional[str] = None
    hit_candle: Optional[Candle] = None
    ambiguous = False

    for idx, c in enumerate(window):
        # 入场 bar: 未知高/低先后 → 若两边都触则标 AMBIGUOUS
        is_entry_bar = (
            c.open_time_ms <= record.opened_at_ms
            < (c.close_time_ms or (c.open_time_ms + HOUR_MS))
        )
        favorable = (c.high - record.entry_price) if direction == "LONG" else (record.entry_price - c.low)
        adverse = (record.entry_price - c.low) if direction == "LONG" else (c.high - record.entry_price)
        mfe = max(mfe, favorable / atr_used)
        mae = max(mae, adverse / atr_used)

        touched_target = c.high >= target if direction == "LONG" else c.low <= target
        touched_stop = c.low <= stop if direction == "LONG" else c.high >= stop

        if touched_target and touched_stop:
            ambiguous = True
            hit = "wrong" if AMBIGUOUS_COUNTS_AS == "wrong" else "correct"
            hit_candle = c
            break
        if is_entry_bar and (touched_target or touched_stop):
            # 入场 bar 未知先后 → 不确定，不给确定性对错
            record.status = SettleStatus.INVALID.value
            record.settled_at_ms = now
            record.settle_detail = {
                "reason": "ambiguous_entry_bar",
                "uncertain": True,
                "ambiguous": True,
                "window_truncated": truncated,
            }
            return record
        if touched_stop:
            hit, hit_candle = "wrong", c
            break
        if touched_target:
            hit, hit_candle = "correct", c
            break

    record.max_favorable_atr = round(mfe, 3)
    record.max_adverse_atr = round(mae, 3)

    if hit is not None and hit_candle is not None:
        record.status = (SettleStatus.CORRECT if hit == "correct" else SettleStatus.WRONG).value
        record.exit_price = target if hit == "correct" else stop
        record.settled_at_ms = hit_candle.close_time_ms or hit_candle.open_time_ms
        record.settle_detail = {
            "reason": "target_first" if hit == "correct" else "stop_first",
            "target": round(target, 4),
            "stop": round(stop, 4),
            "atr_used": round(atr_used, 4),
            "atr_source": "record" if record.atr else "fallback_pct",
            "ambiguous": ambiguous,
            "bars_used": len(window),
            "bars_to_hit": window.index(hit_candle) + 1,
            "window_truncated": truncated,
        }
        return record

    # 窗口走完两边都没触
    if now < window_end:
        record.status = SettleStatus.PENDING.value
        record.settle_detail = {"reason": "window_not_elapsed",
                                "window_end_ms": window_end}
        return record

    last = window[-1]
    record.status = SettleStatus.INVALID.value
    record.exit_price = last.close
    record.settled_at_ms = now
    record.settle_detail = {
        "reason": "chop_no_touch",
        "target": round(target, 4),
        "stop": round(stop, 4),
        "atr_used": round(atr_used, 4),
        "bars_used": len(window),
        "window_truncated": truncated,
    }
    return record


def backfill_atr(record: TradeRecord, candles: Sequence[Candle]) -> None:
    """入场时没记 ATR 的话, 用入场前的 K 线补算一个, 好过用固定百分比兜底."""
    if record.atr and record.atr > 0:
        return
    before = [c for c in candles if c.open_time_ms <= record.opened_at_ms]
    if not before:
        return
    period = int(SETTLE_CONFIG[record.horizon].get("atr_period", 14))
    value = _atr_from_candles(before, period)
    if value:
        record.atr = value


# --------------------------------------------------------------------------- 批量
async def settle_pending(
    journal,
    collector=None,
    now: Optional[int] = None,
    horizon: Optional[str] = None,
) -> Dict[str, Any]:
    """结算所有 PENDING 档案. 按 (horizon, interval) 分组, 每个组合只拉一次 K 线."""
    from collectors.binance_klines import BinanceKlinesCollector

    now = now if now is not None else _now_ms()
    own_collector = collector is None
    klines = collector or BinanceKlinesCollector()

    targets = [r for r in journal.pending() if horizon is None or r.horizon == horizon]
    by_interval: Dict[str, List[TradeRecord]] = {}
    for rec in targets:
        by_interval.setdefault(SETTLE_CONFIG[rec.horizon]["kline_interval"], []).append(rec)

    summary: Dict[str, Any] = {"considered": len(targets), "settled": {}, "fetched": {}}
    try:
        for interval, recs in by_interval.items():
            bar_h = max(interval_hours(interval), 0.01)
            longest = max(SETTLE_CONFIG[r.horizon]["window_hours"] for r in recs)
            limit = min(1500, max(80, int(longest / bar_h) * 3 + 30))
            try:
                candles = await klines.fetch_klines(interval, limit)
            except Exception as exc:
                logger.warning("settle: 拉取 %s K 线失败: %s", interval, exc)
                summary["fetched"][interval] = f"error: {exc}"
                continue
            summary["fetched"][interval] = len(candles)
            for rec in recs:
                backfill_atr(rec, candles)
                settle_record(rec, candles, now=now)
                journal.update(rec)
                key = rec.status
                summary["settled"][key] = summary["settled"].get(key, 0) + 1
    finally:
        if own_collector and hasattr(klines, "close"):
            await klines.close()

    return summary
