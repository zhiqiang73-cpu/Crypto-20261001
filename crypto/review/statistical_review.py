"""统计复盘引擎 — 纯算法, 零外部依赖, 不需要任何 API Key.

背景
----
原实现把「读交易档案 → 提出调参建议」这一步交给外部 LLM (DeepSeek)。
本模块用确定性统计规则替代它, 使自我递归改进闭环在**完全离线**的前提下运转。

安全边界与原先完全一致
----------------------
本模块只负责**产出候选改动**。所有输出仍然必须经过
`review_loop.validate_changes()` 同一套护栏 (白名单 / 区间夹取 / 相对幅度 /
权重组归一 / 阈值不交叉), 且只有 `accept_proposal()` 才会写入新版本。

设计原则
--------
1. **只动有证据的参数** — 样本不足的组一律不参与, 宁可不调也不瞎调。
2. **相对判别力而非绝对水平** — 一组权重里比同组平均更会区分赢单的面加权,
   比平均更差的减权。绝对水平普遍偏高是市场状态, 不是该调的信号。
3. **对称失效单独处理** — 带符号均值会正负相消; 错单里读数幅度明显更大的面,
   即便带符号判别力为正也要抑制其调整幅度。
4. **确定性** — 同样的档案输入必然产出同样的建议, 可复现、可测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.review import (
    PROPOSAL_GUARD,
    TUNABLE_PARAMS,
    WEIGHT_GROUPS,
)
from models.review import ReviewStats, TradeRecord

logger = logging.getLogger(__name__)

FACE_KEYS = ["news", "data", "tech", "prediction"]
FACE_LABELS = {"news": "消息面", "data": "数据面", "tech": "技术面", "prediction": "预测面"}

# --------------------------------------------------------------------------- 调参常量
# 全部集中在此, 便于测试钉死与人工审阅。
MIN_SAMPLE_FOR_WEIGHT_RULE = 20   # 每个 horizon 至少多少有效样本才动权重
MIN_SAMPLE_FOR_TIER_RULE = 15     # 每个决策档至少多少有效样本才动阈值
FACE_DELTA_FLOOR = 3.0            # 带符号均值差小于此视为噪声, 不动该面
EXTREME_ABS_DELTA = 8.0           # 幅度差超过此视为「读得越极端越容易错」
EXTREME_MUTE_FACTOR = 0.5         # 命中对称失效时, 调整幅度打这个折
TILT_STEP = 0.15                  # 权重单次最大倾斜比例
TIER_LOW_WIN_RATE = 0.45          # 档位胜率低于此 → 收紧
TIER_HIGH_WIN_RATE = 0.60         # 档位胜率高于此 → 放宽
TIER_STEP_PCT = 0.08              # 阈值单次调整幅度 (相对当前值的比例)
VALVE_LOW_WIN_RATE = 0.48         # 整体胜率低于此 → 安全阀更保守
VALVE_HIGH_WIN_RATE = 0.58        # 整体胜率高于此 → 安全阀略放松
VALVE_STEP = 3.0                  # 安全阀绝对调整步长 (评分点)
TECH_MULT_STEP = 0.06             # 技术面乘数单次调整比例

# 决策档 → 阈值参数名
TIER_TO_THRESHOLD = {
    "strong_long": "DECISION_THRESHOLDS.strong_long",
    "standard_long": "DECISION_THRESHOLDS.standard_long",
    "watch_long": "DECISION_THRESHOLDS.watch_long",
    "standard_short": "DECISION_THRESHOLDS.standard_short",
    "strong_short": "DECISION_THRESHOLDS.strong_short",
}
SHORT_TIERS = {"standard_short", "strong_short"}


@dataclass
class StatisticalProposal:
    """统计引擎的一次产出. 结构与 LLM 版本保持一致, 便于下游无缝替换."""

    changes: List[Dict[str, Any]] = field(default_factory=list)
    diagnosis: str = ""
    risks: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "changes": self.changes,
            "diagnosis": self.diagnosis,
            "risks": self.risks,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- 工具
def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def _candidate(
    param: str,
    current: float,
    proposed: float,
    rationale: str,
    expected_effect: str,
    confidence: float,
    strength: float,
) -> Dict[str, Any]:
    """构造一条候选改动. strength 仅用于本模块内部排序, 不外传."""
    return {
        "param": param,
        "current": current,
        "proposed": proposed,
        "rationale": rationale,
        "expected_effect": expected_effect,
        "confidence": confidence,
        "_strength": strength,
    }


# --------------------------------------------------------------------------- 规则 1
def _rule_dimension_weights(
    stats: ReviewStats,
    current: Dict[str, float],
    diagnosis: List[str],
) -> List[Dict[str, Any]]:
    """按「相对判别力」调整各 horizon 的四面权重.

    判别力定义: 该面在**赢单**里的平均读数 减去 在**错单**里的平均读数。
    正值 = 这个面确实能区分赢单 → 相对同组其他面加权。
    用相对值而非绝对值, 是因为所有面的读数普遍偏高只反映市场状态, 不是调参信号。
    """
    out: List[Dict[str, Any]] = []

    for horizon, paths in WEIGHT_GROUPS.items():
        bucket = (stats.by_horizon or {}).get(horizon) or {}
        valid = int(bucket.get("valid") or 0)
        if valid < MIN_SAMPLE_FOR_WEIGHT_RULE:
            diagnosis.append(
                f"{horizon} 权重：有效样本 {valid}/{MIN_SAMPLE_FOR_WEIGHT_RULE}，"
                f"未达门槛，本轮不动。"
            )
            continue

        deltas: Dict[str, float] = {}
        extremes: Dict[str, Optional[float]] = {}
        for path in paths:
            face = path.rsplit(".", 1)[-1]
            fm = (stats.face_means or {}).get(face) or {}
            d = _f(fm.get("delta"))
            if d is None or abs(d) < FACE_DELTA_FLOOR:
                continue
            deltas[path] = d
            extremes[path] = _f(fm.get("abs_delta"))

        if len(deltas) < 2:
            diagnosis.append(
                f"{horizon} 权重：可比较的面不足（{len(deltas)} 个达到判别力下限 "
                f"{FACE_DELTA_FLOOR}），本轮不动。"
            )
            continue

        mean_delta = sum(deltas.values()) / len(deltas)
        spread = max(abs(v - mean_delta) for v in deltas.values()) or 1.0

        best_param, best_delta = max(deltas.items(), key=lambda kv: kv[1])
        worst_param, worst_delta = min(deltas.items(), key=lambda kv: kv[1])
        parts = [
            f"{FACE_LABELS.get(p.rsplit('.', 1)[-1], p)} {_num(v)}"
            for p, v in sorted(deltas.items(), key=lambda kv: -kv[1])
        ]

        for path, d in deltas.items():
            cur = _f(current.get(path))
            if cur is None:
                continue
            face = path.rsplit(".", 1)[-1]
            face_label = FACE_LABELS.get(face, face)
            relative = (d - mean_delta) / spread          # ∈ [-1, 1]
            tilt = TILT_STEP * max(-1.0, min(1.0, relative))

            note = ""
            abs_delta = extremes.get(path)
            if abs_delta is not None and abs_delta > EXTREME_ABS_DELTA:
                tilt *= EXTREME_MUTE_FACTOR
                note = (
                    f"；但错单里该面读数幅度高出 {abs_delta:.1f}，"
                    f"提示「读得越极端越容易错」，调整幅度已打 "
                    f"{EXTREME_MUTE_FACTOR:.0%} 折"
                )

            proposed = cur * (1 + tilt)
            out.append(_candidate(
                param=path,
                current=cur,
                proposed=proposed,
                rationale=(
                    f"{horizon} 组内相对判别力 {relative:+.2f}"
                    f"（该面赢单−错单均值差 {_num(d)}，组内均值 {_num(mean_delta)}）"
                    f"{note}"
                ),
                expected_effect=(
                    f"提高{face_label}在 {horizon} 评分中的话语权"
                    if tilt > 0 else
                    f"降低{face_label}在 {horizon} 评分中的话语权"
                ),
                confidence=min(0.9, 0.45 + valid / 400.0),
                strength=abs(relative),
            ))

        diagnosis.append(
            f"{horizon} 权重（有效 {valid} 笔，胜率 "
            f"{_pct(bucket.get('win_rate'))}）：判别力 "
            + " > ".join(parts)
            + f"；相对均值加权 {FACE_LABELS.get(best_param.rsplit('.', 1)[-1], '')}、"
            f"减权 {FACE_LABELS.get(worst_param.rsplit('.', 1)[-1], '')}"
            f"（{_num(best_delta)} vs {_num(worst_delta)}）。"
        )

    return out


# --------------------------------------------------------------------------- 规则 2
def _rule_decision_thresholds(
    stats: ReviewStats,
    current: Dict[str, float],
    diagnosis: List[str],
) -> List[Dict[str, Any]]:
    """按各决策档的实际胜率收紧/放宽入场阈值.

    胜率明显偏低的档位说明「这一档的门槛不够严」→ 收紧 (多头抬高、空头压低)。
    胜率明显偏高的档位说明门槛过严、漏掉了机会 → 小幅放宽。
    """
    out: List[Dict[str, Any]] = []
    tiers = stats.by_tier or {}
    if not tiers:
        diagnosis.append("决策档：暂无分档统计，本轮不动阈值。")
        return out

    for tier, bucket in sorted(tiers.items()):
        param = TIER_TO_THRESHOLD.get(tier)
        if param is None:
            continue
        valid = int(bucket.get("valid") or 0)
        win_rate = _f(bucket.get("win_rate"))
        if valid < MIN_SAMPLE_FOR_TIER_RULE or win_rate is None:
            continue
        cur = _f(current.get(param))
        spec = TUNABLE_PARAMS.get(param)
        if cur is None or spec is None:
            continue

        is_short = tier in SHORT_TIERS
        if win_rate < TIER_LOW_WIN_RATE:
            # 收紧: 多头阈值往上抬, 空头阈值往更负推
            delta = abs(cur) * TIER_STEP_PCT * (1 if not is_short else -1)
            action = "收紧"
            why = f"胜率 {_pct(win_rate)} 低于 {TIER_LOW_WIN_RATE:.0%} 门槛"
        elif win_rate > TIER_HIGH_WIN_RATE:
            delta = -abs(cur) * TIER_STEP_PCT * (1 if not is_short else -1)
            action = "放宽"
            why = f"胜率 {_pct(win_rate)} 高于 {TIER_HIGH_WIN_RATE:.0%}，门槛偏严"
        else:
            continue

        proposed = min(float(spec["max"]), max(float(spec["min"]), cur + delta))
        if abs(proposed - cur) < 1e-9:
            continue
        out.append(_candidate(
            param=param,
            current=cur,
            proposed=proposed,
            rationale=f"{tier} 档有效 {valid} 笔，{why} → {action}入场门槛",
            expected_effect=f"{tier} 档触发频率{'下降' if action == '收紧' else '上升'}",
            confidence=min(0.85, 0.4 + valid / 300.0),
            strength=abs(win_rate - 0.5) * 0.8,
        ))
        diagnosis.append(
            f"阈值：{tier} 档有效 {valid} 笔、胜率 {_pct(win_rate)} → {action} "
            f"{cur:.2f} → {proposed:.2f}。"
        )

    if not out:
        diagnosis.append("阈值：各档胜率均落在中性区间，本轮不动。")
    return out


# --------------------------------------------------------------------------- 规则 3
def _rule_safety_valve(
    stats: ReviewStats,
    current: Dict[str, float],
    diagnosis: List[str],
) -> List[Dict[str, Any]]:
    """整体胜率驱动的安全阀微调."""
    out: List[Dict[str, Any]] = []
    param = "SAFETY_VALVE_THRESHOLD"
    spec = TUNABLE_PARAMS.get(param)
    cur = _f(current.get(param))
    win_rate = _f(stats.win_rate)
    if spec is None or cur is None or win_rate is None or stats.valid < MIN_SAMPLE_FOR_TIER_RULE:
        return out

    if win_rate < VALVE_LOW_WIN_RATE:
        delta, why = VALVE_STEP, f"整体胜率 {_pct(win_rate)} 偏低"
    elif win_rate > VALVE_HIGH_WIN_RATE:
        delta, why = -VALVE_STEP, f"整体胜率 {_pct(win_rate)} 偏高"
    else:
        diagnosis.append(
            f"安全阀：整体胜率 {_pct(win_rate)} 处于中性区间，本轮不动。"
        )
        return out

    proposed = min(float(spec["max"]), max(float(spec["min"]), cur + delta))
    if abs(proposed - cur) < 1e-9:
        return out
    out.append(_candidate(
        param=param,
        current=cur,
        proposed=proposed,
        rationale=f"{why}（{stats.valid} 笔有效样本），安全阀 {cur:.0f} → {proposed:.0f}",
        expected_effect="提高开仓门槛" if delta > 0 else "略微降低开仓门槛",
        confidence=min(0.8, 0.4 + stats.valid / 300.0),
        strength=0.35,
    ))
    diagnosis.append(f"安全阀：{why}，{cur:.0f} → {proposed:.0f}。")
    return out


# --------------------------------------------------------------------------- 规则 4
def _rule_tech_multipliers(
    stats: ReviewStats,
    current: Dict[str, float],
    diagnosis: List[str],
) -> List[Dict[str, Any]]:
    """技术面乘数随技术面的判别力方向微调.

    ADX / 布林收口 这类乘数只在特定市场状态下放大技术面话语权。
    如果技术面整体是反向判别 (delta < 0), 就不该继续放大它。
    """
    out: List[Dict[str, Any]] = []
    fm = (stats.face_means or {}).get("tech") or {}
    d = _f(fm.get("delta"))
    if d is None or abs(d) < FACE_DELTA_FLOOR:
        return out

    direction = 1.0 if d > 0 else -1.0
    for param in ("ADX_BOOST", "BOLL_SQUEEZE_BOOST", "ADX_DAMPEN"):
        spec = TUNABLE_PARAMS.get(param)
        cur = _f(current.get(param))
        if spec is None or cur is None:
            continue
        step = cur * TECH_MULT_STEP * direction
        proposed = min(float(spec["max"]), max(float(spec["min"]), cur + step))
        if abs(proposed - cur) < 1e-9:
            continue
        out.append(_candidate(
            param=param,
            current=cur,
            proposed=proposed,
            rationale=(
                f"技术面判别力 {_num(d)} → "
                f"{'放大' if direction > 0 else '抑制'}该乘数"
            ),
            expected_effect="技术面在趋势/收口行情中权重上升"
            if direction > 0 else "技术面在趋势/收口行情中权重下降",
            confidence=0.4,
            strength=0.25,
        ))
    if out:
        diagnosis.append(
            f"技术乘数：技术面判别力 {_num(d)}，"
            f"{'上调' if direction > 0 else '下调'} ADX / 布林收口乘数。"
        )
    return out


# --------------------------------------------------------------------------- 主入口
def propose_from_stats(
    stats: ReviewStats,
    current: Dict[str, float],
    records: Sequence[TradeRecord] = (),
) -> StatisticalProposal:
    """由统计信号推导一组候选参数改动.

    返回的 changes 已按强度排序并截断到护栏允许的条数; 仍须交给
    `validate_changes()` 做最终夹取与归一。
    """
    diagnosis: List[str] = []
    notes: List[str] = []

    head = (
        f"样本 {stats.total} 笔（有效 {stats.valid}，正确 {stats.correct}，"
        f"错误 {stats.wrong}），胜率 {_pct(stats.win_rate)}。"
    )

    candidates: List[Dict[str, Any]] = []
    for rule in (
        _rule_dimension_weights,
        _rule_decision_thresholds,
        _rule_safety_valve,
        _rule_tech_multipliers,
    ):
        try:
            candidates.extend(rule(stats, current, diagnosis))
        except Exception as exc:                     # 单条规则失败不应拖垮整轮复盘
            logger.warning("statistical_review: 规则 %s 失败: %s", rule.__name__, exc)
            notes.append(f"规则 {rule.__name__} 执行失败: {exc}")

    limit = int(PROPOSAL_GUARD.get("max_params_per_set") or 5)
    candidates.sort(key=lambda c: c["_strength"], reverse=True)
    kept, dropped = candidates[:limit], candidates[limit:]
    if dropped:
        notes.append(
            f"候选 {len(candidates)} 条超过护栏上限 {limit} 条，"
            f"按信号强度保留前 {limit} 条（权重组内的联动项由护栏自动补齐，不占额度）"
        )

    for c in kept:
        c.pop("_strength", None)

    if not kept:
        diagnosis.append("本轮没有任何规则给出足够强的信号，建议维持当前参数。")

    proposal = StatisticalProposal(
        changes=kept,
        diagnosis=head + " " + " ".join(diagnosis),
        risks=(
            "纯统计复盘只读交易档案，无法识别制度性变化（政策转向、流动性枯竭、"
            "交易所规则调整）；它优化的是历史分布的拟合，不保证未来有效。"
            "所有改动仍受相对幅度、权重组归一、阈值不交叉三重护栏约束，"
            "并且只有人工/元评审采纳后才会生效。"
        ),
        notes=notes,
    )
    return proposal


# --------------------------------------------------------------------------- 辅助产出
def annotate_record(rec: TradeRecord, recent_stats: Optional[Dict[str, Any]] = None) -> str:
    """给一笔错单写统计注解 (替代原先的 LLM 注解).

    不做因果推断, 只把「这笔单子为什么看起来是错的」用可核对的数字讲清楚。
    """
    parts: List[str] = ["[统计]"]
    scores = rec.scores or {}
    if scores:
        ordered = sorted(scores.items(), key=lambda kv: -abs(float(kv[1] or 0)))
        top = ordered[:2]
        parts.append(
            "读数最极端的两面：" + "、".join(
                f"{FACE_LABELS.get(k, k)} {float(v):+.1f}" for k, v in top
            ) + "。"
        )
    if rec.decision:
        parts.append(f"触发档位 {rec.decision}，方向 {rec.direction}。")
    if rec.max_favorable_atr is not None and rec.max_adverse_atr is not None:
        parts.append(
            f"持仓期最大有利 {rec.max_favorable_atr:.2f} ATR、"
            f"最大不利 {rec.max_adverse_atr:.2f} ATR。"
        )
        if rec.max_adverse_atr > rec.max_favorable_atr:
            parts.append("不利幅度大于有利幅度，属于入场后即被反向验证。")
        else:
            parts.append("曾出现足够有利幅度但未兑现，属出场时机问题。")
    if recent_stats:
        wr = recent_stats.get("win_rate")
        if wr is not None:
            parts.append(f"同期整体胜率 {float(wr) * 100:.1f}%。")
    return " ".join(parts)[:800]


def build_daily_summary(
    date_str: str,
    records: Sequence[TradeRecord],
    stats: ReviewStats,
    weights: Dict[str, float],
    convergence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成一份确定性的日终总结 (替代原先的 LLM 日总结)."""
    by_horizon = stats.by_horizon or {}
    by_direction = stats.by_direction or {}
    face_means = stats.face_means or {}

    highlights: List[str] = []
    for horizon, bucket in sorted(by_horizon.items()):
        highlights.append(
            f"{horizon}：{bucket.get('valid', 0)} 笔，胜率 "
            f"{_pct(bucket.get('win_rate'))}"
        )
    for direction, bucket in sorted(by_direction.items()):
        highlights.append(
            f"{direction} 方向：{bucket.get('valid', 0)} 笔，胜率 "
            f"{_pct(bucket.get('win_rate'))}，"
            f"平均盈亏 {_num(_f(bucket.get('pnl_atr_avg')))} ATR"
        )

    strongest = None
    ranked = [
        (k, _f((v or {}).get("delta")))
        for k, v in face_means.items()
    ]
    ranked = [(k, d) for k, d in ranked if d is not None]
    if ranked:
        ranked.sort(key=lambda kv: -kv[1])
        strongest = {
            "best": {"face": ranked[0][0], "label": FACE_LABELS.get(ranked[0][0], ""),
                     "delta": ranked[0][1]},
            "worst": {"face": ranked[-1][0], "label": FACE_LABELS.get(ranked[-1][0], ""),
                      "delta": ranked[-1][1]},
        }

    return {
        "date": date_str,
        "engine": "statistical",
        "sample": {
            "trades": len(records),
            "valid": stats.valid,
            "correct": stats.correct,
            "wrong": stats.wrong,
            "win_rate": stats.win_rate,
        },
        "highlights": highlights,
        "face_discrimination": strongest,
        "effective_weights": dict(sorted(weights.items())),
        "convergence": convergence or {},
        "note": "由统计引擎生成，未调用任何外部模型。",
    }
