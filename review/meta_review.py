"""元评审 — 自我递归改进的收敛控制 (纯算法, 不调 DeepSeek).

机制:
  1. 学习率衰减 — 每采纳一版, max_relative_change × DECAY_FACTOR
  2. 振荡锁 — 参数来回改 → 锁定若干版本
  3. 性能门 — 新版胜率不得明显差于旧版 (采纳前用提案置信度; 采纳后用实盘)
  4. 自动回滚 — 新版跑满 N 笔后若胜率掉幅过大 → 回滚
  5. 观察模式 — 连续回滚过多 → 停止自动调参
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from config.review import (
    DECAY_FACTOR,
    INITIAL_MAX_RELATIVE_CHANGE,
    MAX_CONSECUTIVE_ROLLBACKS,
    META_STATE_PATH,
    MIN_MAX_RELATIVE_CHANGE,
    OSCILLATION_LOCK_VERSIONS,
    OSCILLATION_LOOKBACK,
    OSCILLATION_SIGN_CHANGES,
    PERF_GATE_TOLERANCE,
    ROLLBACK_SAMPLE_SIZE,
    ROLLBACK_WINRATE_DROP,
    VALID_SAMPLE_TARGET,
)
from models.review import ProposalSet, ReviewStats, TradeRecord
from review import overrides
from review.stats import compute_stats

logger = logging.getLogger(__name__)


@dataclass
class VersionPerformanceCard:
    version: str
    trades_count: int = 0
    valid_count: int = 0
    win_rate: Optional[float] = None
    sharpe_like: Optional[float] = None
    max_drawdown_atr: float = 0.0
    face_contribution: Dict[str, Any] = field(default_factory=dict)
    regime_tag: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MetaState:
    observation_mode: bool = False
    consecutive_rollbacks: int = 0
    locked_params: Dict[str, int] = field(default_factory=dict)  # param → 剩余锁版本数
    version_index: int = 0
    last_rollback_reason: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetaState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_meta_state(path: Optional[Path] = None) -> MetaState:
    p = path or META_STATE_PATH
    if not p.exists():
        return MetaState()
    try:
        return MetaState.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("meta_state load: %s", exc)
        return MetaState()


def save_meta_state(state: MetaState, path: Optional[Path] = None) -> None:
    p = path or META_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def tuned_version_count() -> int:
    """已采纳的调参版本数 (不含 baseline)."""
    n = 0
    for v in overrides.list_versions():
        if v.get("kind") == "tuned":
            n += 1
    return n


def current_max_relative_change(version_index: Optional[int] = None) -> float:
    """学习率衰减后的单参数相对改动上限."""
    idx = version_index if version_index is not None else tuned_version_count()
    rate = INITIAL_MAX_RELATIVE_CHANGE * (DECAY_FACTOR ** idx)
    return max(MIN_MAX_RELATIVE_CHANGE, rate)


def detect_oscillation(versions: Optional[List[Dict[str, Any]]] = None) -> Set[str]:
    """最近 LOOKBACK 个版本中, 某参数方向反转 >= SIGN_CHANGES → 锁定."""
    versions = versions if versions is not None else overrides.list_versions()
    tuned = [v for v in versions if v.get("kind") == "tuned"]
    recent = tuned[-OSCILLATION_LOOKBACK:]
    if len(recent) < 3:
        return set()

    # param → list of deltas in chronological order
    deltas: Dict[str, List[float]] = {}
    for v in recent:
        seen = set()
        for ch in v.get("changes") or []:
            param = ch.get("param")
            if not param or param in seen:
                continue
            seen.add(param)
            deltas.setdefault(param, []).append(float(ch.get("delta") or 0))

    locked: Set[str] = set()
    for param, ds in deltas.items():
        if len(ds) < 3:
            continue
        sign_changes = sum(
            1 for a, b in zip(ds, ds[1:])
            if a * b < 0 and abs(a) > 1e-6 and abs(b) > 1e-6
        )
        if sign_changes >= OSCILLATION_SIGN_CHANGES:
            locked.add(param)
    return locked


def build_performance_card(
    version: str,
    records: Sequence[TradeRecord],
) -> VersionPerformanceCard:
    subset = [r for r in records if (r.config_version or "").startswith(version)
              or r.config_version == version]
    # 宽松匹配: version 标签可能是 "v2" 而记录里是 "v2"
    if not subset:
        subset = [r for r in records if version in (r.config_version or "")]
    stats = compute_stats(subset)
    pnl_vals = []
    for r in subset:
        if r.max_favorable_atr is not None and r.max_adverse_atr is not None:
            pnl_vals.append(r.max_favorable_atr - r.max_adverse_atr)
    sharpe = None
    if len(pnl_vals) >= 5:
        mean = sum(pnl_vals) / len(pnl_vals)
        var = sum((x - mean) ** 2 for x in pnl_vals) / len(pnl_vals)
        std = var ** 0.5
        sharpe = round(mean / std, 3) if std > 1e-9 else None
    max_dd = 0.0
    equity = 0.0
    peak = 0.0
    for x in pnl_vals:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # 简单 regime: 用 |CS| 均值粗分
    abs_cs = [abs(r.composite_score) for r in subset]
    avg_abs = sum(abs_cs) / len(abs_cs) if abs_cs else 0
    if avg_abs >= 40:
        regime = "trending"
    elif avg_abs <= 15:
        regime = "ranging"
    else:
        regime = "volatile"

    return VersionPerformanceCard(
        version=version,
        trades_count=len(subset),
        valid_count=stats.valid,
        win_rate=stats.win_rate,
        sharpe_like=sharpe,
        max_drawdown_atr=round(max_dd, 3),
        face_contribution=stats.face_means,
        regime_tag=regime,
    )


def should_accept_by_parent_perf(
    parent_card: Optional[VersionPerformanceCard],
    proposed_confidence: Optional[float] = None,
) -> tuple[bool, str]:
    """采纳前的轻量性能门 — 主要靠置信度; 实盘对比在回滚阶段."""
    if proposed_confidence is not None and proposed_confidence < 0.35:
        return False, f"模型置信度过低 ({proposed_confidence:.2f} < 0.35)"
    return True, "ok"


def filter_locked_changes(
    proposal: ProposalSet,
    state: MetaState,
) -> ProposalSet:
    """剔除被振荡锁住的参数改动."""
    if not state.locked_params:
        return proposal
    kept = []
    dropped = []
    for ch in proposal.changes:
        remaining = state.locked_params.get(ch.param, 0)
        if remaining > 0:
            dropped.append(ch.param)
        else:
            kept.append(ch)
    if dropped:
        proposal.changes = kept
        note = f"振荡锁剔除: {', '.join(dropped)}"
        proposal.risks = ((proposal.risks or "") + "\n" + note).strip()
        if not kept:
            proposal.status = "blocked"
            proposal.blocked_reason = note
    return proposal


def apply_learning_rate_clamp(
    proposal: ProposalSet,
    current_params: Dict[str, float],
    max_rel: float,
) -> ProposalSet:
    """用当前衰减后的学习率二次夹取."""
    for ch in proposal.changes:
        cur = float(current_params.get(ch.param, ch.current))
        if not cur:
            continue
        lo, hi = cur * (1 - max_rel), cur * (1 + max_rel)
        if ch.proposed < lo:
            ch.proposed = round(lo, 6)
            ch.clamped = True
            ch.clamp_note = (ch.clamp_note + f"; lr≤{max_rel:.0%}").strip("; ")
        elif ch.proposed > hi:
            ch.proposed = round(hi, 6)
            ch.clamped = True
            ch.clamp_note = (ch.clamp_note + f"; lr≤{max_rel:.0%}").strip("; ")
    return proposal


def tick_locks_after_accept(state: MetaState) -> MetaState:
    """采纳后: 锁计数 -1; 检测新振荡并加锁."""
    new_locked = {}
    for p, n in state.locked_params.items():
        if n - 1 > 0:
            new_locked[p] = n - 1
    for p in detect_oscillation():
        new_locked[p] = max(new_locked.get(p, 0), OSCILLATION_LOCK_VERSIONS)
    state.locked_params = new_locked
    state.version_index = tuned_version_count()
    return state


def check_rollback(
    records: Sequence[TradeRecord],
    state: MetaState,
) -> tuple[bool, str, Optional[str]]:
    """若当前生效版相对父版退步过大 → (should_rollback, reason, parent_version)."""
    if state.observation_mode:
        return False, "observation_mode", None
    active = overrides.active_doc()
    if not active or active.get("kind") != "tuned":
        return False, "no_tuned_active", None
    parent = active.get("parent")
    if not parent:
        return False, "no_parent", None

    curr_card = build_performance_card(active["version"], records)
    if curr_card.valid_count < ROLLBACK_SAMPLE_SIZE:
        return False, f"samples {curr_card.valid_count}/{ROLLBACK_SAMPLE_SIZE}", None

    parent_card = build_performance_card(parent, records)
    if parent_card.win_rate is None or curr_card.win_rate is None:
        return False, "missing_winrate", None

    drop = parent_card.win_rate - curr_card.win_rate
    if drop >= ROLLBACK_WINRATE_DROP:
        reason = (
            f"{active['version']} 胜率 {curr_card.win_rate:.1%} "
            f"较 {parent} 的 {parent_card.win_rate:.1%} 低 {drop:.1%}"
        )
        return True, reason, parent
    return False, "ok", None


def enter_observation_if_needed(state: MetaState) -> MetaState:
    if state.consecutive_rollbacks >= MAX_CONSECUTIVE_ROLLBACKS:
        state.observation_mode = True
        state.last_rollback_reason = (
            f"连续回滚 {state.consecutive_rollbacks} 次, 进入观察模式"
        )
        logger.warning("meta: %s", state.last_rollback_reason)
    return state


def convergence_status(
    records: Sequence[TradeRecord],
    state: Optional[MetaState] = None,
) -> Dict[str, Any]:
    state = state or load_meta_state()
    stats = compute_stats(records)
    active = overrides.current_version_label()
    card = build_performance_card(
        overrides.active_version_name() or "v1", records
    )
    return {
        "observation_mode": state.observation_mode,
        "consecutive_rollbacks": state.consecutive_rollbacks,
        "locked_params": state.locked_params,
        "learning_rate": current_max_relative_change(state.version_index),
        "version_index": state.version_index,
        "active_version": active,
        "valid_samples": stats.valid,
        "valid_target": VALID_SAMPLE_TARGET,
        "ready_for_review": stats.ready and not state.observation_mode,
        "performance": card.to_dict(),
        "last_rollback_reason": state.last_rollback_reason,
        "oscillating_now": sorted(detect_oscillation()),
        "auto_accept": True,
        "history": state.history[-10:],
    }


def gate_proposal(
    proposal: ProposalSet,
    current_params: Dict[str, float],
    state: Optional[MetaState] = None,
) -> tuple[ProposalSet, MetaState, str]:
    """对提案施加元评审约束. 返回 (提案, 状态, 说明)."""
    state = state or load_meta_state()
    if state.observation_mode:
        proposal.status = "blocked"
        proposal.blocked_reason = "观察模式: 停止自动调参"
        return proposal, state, proposal.blocked_reason

    proposal = filter_locked_changes(proposal, state)
    if not proposal.changes:
        return proposal, state, proposal.blocked_reason or "无可用改动"

    max_rel = current_max_relative_change(state.version_index)
    proposal = apply_learning_rate_clamp(proposal, current_params, max_rel)

    # 置信度门: 取最低 confidence
    confs = [c.confidence for c in proposal.changes if c.confidence is not None]
    min_conf = min(confs) if confs else None
    ok, why = should_accept_by_parent_perf(None, min_conf)
    if not ok:
        proposal.status = "blocked"
        proposal.blocked_reason = why
        return proposal, state, why

    return proposal, state, f"通过门控 (lr={max_rel:.0%})"
