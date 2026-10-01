"""复盘回路 — 触发条件、调用模型、护栏校验、采纳与版本留档.

职责边界:
  * 本模块**只出建议**, 不自动改任何生效参数。
  * 所有模型输出必须过 validate_changes() 护栏才会被展示。
  * 只有 accept_proposal() 才会写新版本, 且旧版自动留档可回滚。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.review import (
    MIN_NEW_ERRORS_FOR_RERUN,
    PROPOSAL_GUARD,
    PROPOSALS_DIR,
    THRESHOLD_ORDER,
    TUNABLE_PARAMS,
    VALID_SAMPLE_TARGET,
    WEIGHT_GROUPS,
)
from models.review import (
    ParamChange,
    ProposalSet,
    ProposalStatus,
    ReviewStats,
    TradeRecord,
    now_ms,
)
from review import deepseek as ds
from review import overrides
from review.prompt import build_error_annotation_messages, build_review_messages
from review.stats import compute_stats

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- 护栏
def _normalize_group(values: Dict[str, float], paths: List[str]) -> Optional[Dict[str, float]]:
    """把一组权重缩到和为 1.0, 同时尊重各自 [min, max].

    只对「还夹在边界内」的成员做比例缩放, 已顶到边界的成员保持不动 —
    否则夹取会再次破坏和。迭代收敛, 而非一次除法。
    """
    vals = {p: float(values[p]) for p in paths}
    mins = {p: float(TUNABLE_PARAMS[p]["min"]) for p in paths}
    maxs = {p: float(TUNABLE_PARAMS[p]["max"]) for p in paths}

    for _ in range(24):
        total = sum(vals.values())
        if total <= 0:
            return None
        if abs(total - 1.0) <= 1e-9:
            break
        free = [p for p in paths if mins[p] < vals[p] < maxs[p]]
        if not free:
            break                       # 全被夹死, 只能停在最接近的状态
        frozen = sum(vals[p] for p in paths if p not in free)
        free_sum = sum(vals[p] for p in free)
        ratio = (1.0 - frozen) / free_sum
        if ratio <= 0:
            break
        for p in free:
            vals[p] = min(maxs[p], max(mins[p], vals[p] * ratio))
    return vals


def _round_group_to_one(values: Dict[str, float], paths: List[str]) -> Dict[str, float]:
    """定点到 6 位小数, 并让最大的一项吸收舍入残差, 保证和仍严格为 1.0.

    直接各项 round(4) 会让四个数加起来是 1.0001 —— 比护栏自己的 1e-6 容差还松,
    下次复盘就会反复触发「归一」, 引擎的 CS 也会被悄悄放大万分之几。
    """
    rounded = {p: round(float(values[p]), 6) for p in paths}
    anchor = max(paths, key=lambda p: rounded[p])
    others = sum(v for p, v in rounded.items() if p != anchor)
    fixed = round(1.0 - others, 6)
    spec = TUNABLE_PARAMS[anchor]
    rounded[anchor] = min(float(spec["max"]), max(float(spec["min"]), fixed))
    return rounded


def validate_changes(
    raw_changes: Sequence[Dict[str, Any]],
    current: Dict[str, float],
    max_relative_change: Optional[float] = None,
) -> Tuple[List[ParamChange], List[str]]:
    """把模型给的改动过一遍护栏. 返回 (可用改动, 被拒说明).

    依次施加:
      1. 白名单 — 不在 TUNABLE_PARAMS 里的直接丢弃
      2. 类型/有限性 — 非数字或 NaN/inf 丢弃
      3. 数量上限 — 模型建议最多收前 max_params_per_set 条
      4. 绝对区间 [min, max] 夹取
      5. 相对幅度 ±max_relative_change 夹取 (可由元评审注入衰减后的学习率)
      6. 权重组归一 — 整组一起缩, 保证和恒为 1.0 (可能补出「归一联动」条目)
      7. 阈值单调 — 保证档位不交叉

    注: 上限只约束**模型提出的**条目; 归一联动是数学上的必要修正, 不计入上限,
    否则截断会把一组权重切缺, 反而破坏「和 = 1.0」这条硬约束。
    """
    guard = PROPOSAL_GUARD
    limit = guard["max_params_per_set"]
    notes: List[str] = []
    out: List[ParamChange] = []
    overflowed = False

    for raw in raw_changes or []:
        if not isinstance(raw, dict):
            notes.append(f"丢弃非对象条目: {str(raw)[:60]}")
            continue
        name = str(raw.get("param") or "").strip()
        spec = TUNABLE_PARAMS.get(name)
        if spec is None:
            notes.append(f"丢弃白名单外参数: {name or '(空)'}")
            continue
        if name in {c.param for c in out}:
            notes.append(f"丢弃重复参数: {name}")
            continue
        if len(out) >= limit:
            overflowed = True
            continue
        try:
            proposed = float(raw.get("proposed"))
        except (TypeError, ValueError):
            notes.append(f"丢弃非数值建议: {name}={raw.get('proposed')!r}")
            continue
        if proposed != proposed or proposed in (float("inf"), float("-inf")):
            notes.append(f"丢弃非法数值: {name}")
            continue

        cur = float(current.get(name, spec["min"]))
        clamped = False
        reasons: List[str] = []

        # 3. 绝对区间
        if proposed < spec["min"]:
            proposed, clamped = float(spec["min"]), True
            reasons.append(f"下限 {spec['min']}")
        elif proposed > spec["max"]:
            proposed, clamped = float(spec["max"]), True
            reasons.append(f"上限 {spec['max']}")

        # 4. 相对幅度 (支持元评审注入的衰减学习率)
        max_rel = (
            float(max_relative_change)
            if max_relative_change is not None
            else float(guard["max_relative_change"])
        )
        if cur:
            lo, hi = cur * (1 - max_rel), cur * (1 + max_rel)
            if proposed < lo:
                proposed, clamped = lo, True
                reasons.append(f"幅度限 {max_rel:.0%}")
            elif proposed > hi:
                proposed, clamped = hi, True
                reasons.append(f"幅度限 {max_rel:.0%}")

        precision = 4 if spec["group"] != "threshold" else 2
        out.append(ParamChange(
            param=name,
            current=cur,
            proposed=round(proposed, precision),
            rationale=str(raw.get("rationale") or "")[:600],
            expected_effect=str(raw.get("expected_effect") or "")[:400],
            confidence=_clamp_confidence(raw.get("confidence")),
            clamped=clamped,
            clamp_note="; ".join(reasons),
        ))

    # 6. 权重组归一 (整组一起缩, 否则和回不到 1.0)
    for group, paths in WEIGHT_GROUPS.items():
        touched = [c for c in out if c.param in paths]
        if not touched:
            continue
        by_param = {c.param: c for c in touched}
        affected = {
            p: (by_param[p].proposed if p in by_param
                else float(current.get(p, TUNABLE_PARAMS[p]["min"])))
            for p in paths
        }
        before = sum(affected.values())
        normalized = _normalize_group(affected, paths)
        if normalized is None:
            notes.append(f"权重组 {group} 归一失败 (和 <= 0), 整组丢弃")
            out = [c for c in out if c.param not in paths]
            continue
        normalized = _round_group_to_one(normalized, paths)
        note_txt = f"权重组 {group} 归一 (原和 {before:.4f})"
        for p in paths:
            new_val = normalized[p]
            change = by_param.get(p)
            if change is None:
                # 模型没提这一项, 但为了让整组和为 1.0 必须跟着动 → 补联动条目
                out.append(ParamChange(
                    param=p,
                    current=float(current.get(p, TUNABLE_PARAMS[p]["min"])),
                    proposed=new_val,
                    rationale=f"权重组 {group} 归一联动",
                    clamped=True,
                    clamp_note=note_txt,
                ))
                continue
            change.proposed = new_val
            change.clamped = True
            change.clamp_note = "; ".join(x for x in (change.clamp_note, note_txt) if x)
        if abs(before - 1.0) > guard["weights_sum_tolerance"]:
            notes.append(f"权重组 {group} 触发归一, 和 {before:.4f} → 1.0")

    # 7. 阈值单调
    if guard["forbid_threshold_cross"]:
        proposed_map = {c.param: c.proposed for c in out}
        merged = {p: proposed_map.get(p, float(current.get(p, 0))) for p in THRESHOLD_ORDER}
        if not all(merged[a] < merged[b] for a, b in zip(THRESHOLD_ORDER, THRESHOLD_ORDER[1:])):
            notes.append("阈值出现交叉, 本轮全部阈值改动作废")
            out = [c for c in out if c.param not in set(THRESHOLD_ORDER)]

    if overflowed:
        notes.append(f"模型建议条目超过上限, 只接收前 {limit} 条")

    return out, notes


def _clamp_confidence(value: Any) -> Optional[float]:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(1.0, c)), 2)


# --------------------------------------------------------------------------- 触发条件
def can_run(
    stats: ReviewStats,
    since_last_error_count: Optional[int] = None,
) -> Tuple[bool, str]:
    """判断现在能不能跑复盘."""
    if stats.valid < VALID_SAMPLE_TARGET:
        return False, f"有效样本 {stats.valid}/{VALID_SAMPLE_TARGET}, 还差 {stats.remaining} 笔"
    if since_last_error_count is not None and since_last_error_count < MIN_NEW_ERRORS_FOR_RERUN:
        return False, (
            f"上版建议后仅新增 {since_last_error_count} 笔错单, "
            f"攒够 {MIN_NEW_ERRORS_FOR_RERUN} 笔再跑"
        )
    return True, "样本已达标"


# --------------------------------------------------------------------------- 跑复盘
async def run_review(
    client: "ds.DeepSeekClient",
    records: Sequence[TradeRecord],
    extra_context: str = "",
    force: bool = False,
) -> ProposalSet:
    """调用模型产出一次参数建议. force=True 可绕过样本量门槛 (面板「强制演练」)."""
    last = latest_decided_proposal()
    since_ms = last.created_at_ms if last else None
    since_errors = (
        sum(1 for r in records
            if r.status == "wrong" and (since_ms is None or (r.settled_at_ms or 0) >= since_ms))
        if last else None
    )
    stats = compute_stats(records, since_ms=since_ms)

    ok, why = can_run(stats, since_errors)
    if not ok and not force:
        return ProposalSet(
            proposal_id=_new_proposal_id(),
            created_at_ms=now_ms(),
            model=client.model,
            valid_sample_count=stats.valid,
            status=ProposalStatus.BLOCKED.value,
            blocked_reason=why,
            diagnosis=why,
            stats_snapshot=stats.to_dict(),
        )

    current = overrides.effective_params()
    version = overrides.current_version_label()
    # 注入版本绩效卡, 让模型知道上次改了什么、结果如何
    try:
        from review import meta_review as _mr
        card = _mr.build_performance_card(
            overrides.active_version_name() or "v1", records
        )
        lr = _mr.current_max_relative_change()
        extra_context = (
            (extra_context + "\n" if extra_context else "")
            + f"current_learning_rate={lr:.4f}; "
            + f"version_performance={card.to_dict()}"
        )
        max_rel = lr
    except Exception:
        max_rel = None

    messages = build_review_messages(stats, records, current, version, extra_context)
    raw = await client.chat_json(messages)

    changes, notes = validate_changes(
        raw.get("changes") or [], current, max_relative_change=max_rel
    )
    proposal = ProposalSet(
        proposal_id=_new_proposal_id(),
        created_at_ms=now_ms(),
        model=client.model,
        valid_sample_count=stats.valid,
        diagnosis=str(raw.get("diagnosis") or ""),
        changes=changes,
        risks=str(raw.get("risks") or ""),
        stats_snapshot=stats.to_dict(),
        status=ProposalStatus.PENDING.value if changes else ProposalStatus.BLOCKED.value,
        blocked_reason="" if changes else (
            "模型未给出可用改动" + ("；" + "；".join(notes) if notes else "")
        ),
    )
    if notes:
        proposal.risks = (proposal.risks + "\n\n护栏说明: " + "；".join(notes)).strip()
    save_proposal(proposal)
    return proposal


async def annotate_error(
    client: "ds.DeepSeekClient",
    rec: TradeRecord,
    journal=None,
    recent_stats: Optional[Dict[str, Any]] = None,
) -> TradeRecord:
    """给一笔错单写模型分析备注, 并写回 journal."""
    messages = build_error_annotation_messages(rec, recent_stats)
    raw = await client.chat_json(messages)
    parts = []
    face = str(raw.get("primary_face") or "").strip()
    if face:
        parts.append(f"[{face}]")
    if raw.get("what_went_wrong"):
        parts.append(str(raw["what_went_wrong"]))
    if raw.get("note"):
        parts.append(f"提醒: {raw['note']}")
    rec.model_note = " ".join(parts)[:800]
    if journal is not None:
        journal.update(rec)
    return rec


# --------------------------------------------------------------------------- 建议存档
def _new_proposal_id() -> str:
    return f"P{now_ms()}-{uuid.uuid4().hex[:6]}"


def _proposal_path(proposal_id: str) -> Path:
    return PROPOSALS_DIR / f"{proposal_id}.json"


def save_proposal(proposal: ProposalSet) -> Path:
    import json
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = _proposal_path(proposal.proposal_id)
    path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_proposals() -> List[ProposalSet]:
    import json
    if not PROPOSALS_DIR.exists():
        return []
    out: List[ProposalSet] = []
    for f in PROPOSALS_DIR.glob("P*.json"):
        try:
            out.append(ProposalSet.from_dict(json.loads(f.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("review: 跳过损坏建议文件 %s: %s", f.name, exc)
    out.sort(key=lambda p: p.created_at_ms, reverse=True)
    return out


def get_proposal(proposal_id: str) -> Optional[ProposalSet]:
    path = _proposal_path(proposal_id)
    if not path.exists():
        return None
    import json
    return ProposalSet.from_dict(json.loads(path.read_text(encoding="utf-8")))


def latest_decided_proposal() -> Optional[ProposalSet]:
    """最近一次已决 (采纳或驳回) 的建议 — 用来算「上版之后又攒了多少错单」."""
    for p in load_proposals():
        if p.status in (ProposalStatus.ACCEPTED.value, ProposalStatus.REJECTED.value):
            return p
    return None


def accept_proposal(proposal_id: str) -> Dict[str, Any]:
    """采纳: 写入新版本 + 旧版留档. 这是唯一会改动生效参数的入口."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise FileNotFoundError(f"建议 {proposal_id} 不存在")
    if proposal.status == ProposalStatus.ACCEPTED.value:
        raise ValueError(f"建议 {proposal_id} 已采纳 (版本 {proposal.applied_version})")
    if not proposal.changes:
        raise ValueError("该建议没有任何可用改动, 无法采纳")

    doc = overrides.commit_version(proposal.changes, meta={
        "proposal_id": proposal.proposal_id,
        "model": proposal.model,
        "valid_sample_count": proposal.valid_sample_count,
        "diagnosis": proposal.diagnosis,
        "risks": proposal.risks,
    })
    proposal.status = ProposalStatus.ACCEPTED.value
    proposal.applied_version = doc["version"]
    proposal.decided_at_ms = now_ms()
    save_proposal(proposal)
    return doc


def reject_proposal(proposal_id: str) -> ProposalSet:
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise FileNotFoundError(f"建议 {proposal_id} 不存在")
    proposal.status = ProposalStatus.REJECTED.value
    proposal.decided_at_ms = now_ms()
    save_proposal(proposal)
    return proposal
