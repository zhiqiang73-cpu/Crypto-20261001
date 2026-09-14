"""复盘池统计 — 分母恒为「有效样本」(对 + 错).

面板与模型看到的是同一份统计, 避免两边口径不一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from config.review import VALID_SAMPLE_TARGET
from models.review import ReviewStats, SettleStatus, TradeRecord

FACE_KEYS = ["news", "data", "tech", "prediction"]
FACE_LABELS = {"news": "消息面", "data": "数据面", "tech": "技术面", "prediction": "预测面"}


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _bucket() -> Dict[str, int]:
    return {"valid": 0, "correct": 0, "wrong": 0}


def _win_rate(b: Dict[str, int]) -> Optional[float]:
    return round(b["correct"] / b["valid"], 4) if b["valid"] else None


def compute_stats(
    records: Sequence[TradeRecord],
    target: int = VALID_SAMPLE_TARGET,
    since_ms: Optional[int] = None,
) -> ReviewStats:
    """给定全部档案, 算出复盘池统计."""
    st = ReviewStats(target=target, total=len(records))

    by_horizon: Dict[str, Dict[str, int]] = {}
    by_tier: Dict[str, Dict[str, int]] = {}
    by_direction: Dict[str, Dict[str, Any]] = {}
    face_correct: Dict[str, List[float]] = {k: [] for k in FACE_KEYS}
    face_wrong: Dict[str, List[float]] = {k: [] for k in FACE_KEYS}
    # 绝对值单独收一份: 像「读数越极端越容易判错」这种**对称**失效,
    # 带符号的均值会正负相消看不出来, 必须看幅度。
    face_correct_abs: Dict[str, List[float]] = {k: [] for k in FACE_KEYS}
    face_wrong_abs: Dict[str, List[float]] = {k: [] for k in FACE_KEYS}

    for rec in records:
        status = rec.status
        if status == SettleStatus.CORRECT.value:
            st.correct += 1
        elif status == SettleStatus.WRONG.value:
            st.wrong += 1
        elif status == SettleStatus.INVALID.value:
            st.invalid += 1
        elif status == SettleStatus.PENDING.value:
            st.pending += 1
        elif status == SettleStatus.EXCLUDED.value:
            st.excluded += 1
        else:
            continue

        if status not in (SettleStatus.CORRECT.value, SettleStatus.WRONG.value):
            continue

        st.valid += 1
        hb = by_horizon.setdefault(rec.horizon, _bucket())
        hb["valid"] += 1
        hb["correct" if status == SettleStatus.CORRECT.value else "wrong"] += 1

        tb = by_tier.setdefault(rec.decision, _bucket())
        tb["valid"] += 1
        tb["correct" if status == SettleStatus.CORRECT.value else "wrong"] += 1

        dk = rec.direction
        db = by_direction.setdefault(dk, dict(_bucket(), pnl_atr_sum=0.0))
        db["valid"] += 1
        db["correct" if status == SettleStatus.CORRECT.value else "wrong"] += 1
        # 用 MFE/MAE 的差近似这笔的盈亏 (ATR 倍数), 比只看对错更有信息量
        if rec.max_favorable_atr is not None and rec.max_adverse_atr is not None:
            db["pnl_atr_sum"] += (rec.max_favorable_atr - rec.max_adverse_atr)

        bucket = face_correct if status == SettleStatus.CORRECT.value else face_wrong
        bucket_abs = face_correct_abs if status == SettleStatus.CORRECT.value else face_wrong_abs
        for k in FACE_KEYS:
            if k in rec.scores:
                bucket[k].append(rec.scores[k])
                bucket_abs[k].append(abs(rec.scores[k]))

        if status == SettleStatus.WRONG.value and since_ms is not None:
            if rec.settled_at_ms and rec.settled_at_ms >= since_ms:
                st.errors_since_last_review += 1
        elif status == SettleStatus.WRONG.value and since_ms is None:
            st.errors_since_last_review += 1

    st.win_rate = round(st.correct / st.valid, 4) if st.valid else None
    st.remaining = max(0, target - st.valid)
    st.ready = st.valid >= target

    for h, b in by_horizon.items():
        b["win_rate"] = _win_rate(b)          # type: ignore[assignment]
    for t, b in by_tier.items():
        b["win_rate"] = _win_rate(b)          # type: ignore[assignment]
    for d, b in by_direction.items():
        b["win_rate"] = _win_rate(b)
        b["pnl_atr_avg"] = round(b["pnl_atr_sum"] / b["valid"], 3) if b["valid"] else None
        b.pop("pnl_atr_sum", None)

    st.by_horizon = by_horizon
    st.by_tier = by_tier
    st.by_direction = by_direction

    st.face_means = {}
    for k in FACE_KEYS:
        mc, mw = _mean(face_correct[k]), _mean(face_wrong[k])
        ac, aw = _mean(face_correct_abs[k]), _mean(face_wrong_abs[k])
        st.face_means[k] = {
            "label": FACE_LABELS[k],
            # 带符号均值: 看这一面是否在某个方向上系统性偏错
            "correct": mc,
            "wrong": mw,
            "delta": None if mc is None or mw is None else round(mc - mw, 2),
            # 幅度均值: 看这一面是否「读得越极端越容易错」(对称失效)
            "abs_correct": ac,
            "abs_wrong": aw,
            "abs_delta": None if ac is None or aw is None else round(aw - ac, 2),
            "n_correct": len(face_correct[k]),
            "n_wrong": len(face_wrong[k]),
        }

    return st
