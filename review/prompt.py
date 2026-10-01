"""构造送进 DeepSeek 的复盘 payload.

设计原则:
  * 只喂「事实」不喂「期望」 — 给统计与逐笔明细, 不给暗示性结论。
  * 当前值由本地注入 (模型无权声明 current), 模型只出 proposed。
  * 输出强制 json, 且 schema 极窄, 便于机械校验与夹取。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from config.review import MAX_ERRORS_IN_PAYLOAD, MAX_TRADES_IN_PAYLOAD, TUNABLE_PARAMS
from models.review import ReviewStats, TradeRecord

SYSTEM_PROMPT = """你是 BTC/USDT 量化交易系统的参数评审员。

系统用「四面一体」框架给每笔信号打分: CS = Σ W_i × S_i, 四个面分别是
消息面(news)、数据面(data)、技术面(tech)、预测面(prediction), 每面读数
S_i ∈ [-100, +100], CS ∈ [-100, +100]。系统已按事后价格对每笔结算了对错。

你的任务: 只看给定的统计数据与逐笔明细, 判断是否存在系统性的参数错配,
并给出**最小、可解释、可回滚**的参数微调建议。

必须遵守:
1. 只输出 json, 不要输出任何 json 之外的文字。
2. 只能修改 allowed_params 里列出的参数, 一个都不能越界。
3. changes 最多 5 条。宁可少改, 不要大改。没有把握就返回空的 changes。
4. 单项改动幅度不要超过当前值的 ±25%。
5. 维度权重是每套口径一组, 同组四个权重之和必须仍然等于 1.0。
   如果你改了某一组里的权重, 必须同时给出该组其他权重的调整, 使和保持 1.0。
6. 决策阈值必须保持单调: strong_short < standard_short < watch_long
   < standard_long < strong_long, 不得交叉。
7. 样本量小的时候要明说不可靠。有效样本不足时, 优先给「继续观察」而不是改参数。
8. 不要因为单笔亏损就改参数 — 只针对**重复出现的**错法。
9. 有些失效是对称的: 某面读数越极端越容易判错, 但多空各半。
   这种情况下带符号的均值会正负相消, 要看 abs_delta 才能发现。别只看 delta。
10. 输出中文。
"""

OUTPUT_SCHEMA_HINT = {
    "diagnosis": "对错因的整体诊断, 2~4 句, 指出哪一面/哪个档位在系统性失准",
    "changes": [
        {
            "param": "allowed_params 里的参数名, 原样复制",
            "proposed": "建议的新数值, 数字类型",
            "rationale": "为什么改, 引用具体统计数字",
            "expected_effect": "预期改善什么, 以及代价是什么",
            "confidence": "0~1 的小数, 你对这条建议的把握",
        }
    ],
    "risks": "这次调整最大的风险, 尤其是过拟合风险, 1~3 句",
}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _param_spec(name: str, spec: Dict[str, Any], current_params: Dict[str, float]) -> Dict[str, Any]:
    """把白名单条目摊平成模型可读的参数说明."""
    out: Dict[str, Any] = {
        "current": current_params.get(name),
        "min": spec["min"],
        "max": spec["max"],
        "group": spec["group"],
    }
    for extra in ("horizon", "dim"):
        if extra in spec:
            out[extra] = spec[extra]
    return out


def _compact_trade(rec: TradeRecord) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "id": rec.trade_id,
        "opened": _iso(rec.opened_at_ms),
        "horizon": rec.horizon,
        "entry": rec.entry_price,
        "faces": {k: rec.scores.get(k) for k in ("news", "data", "tech", "prediction")},
        "cs": rec.composite_score,
        "decision": rec.decision,
        "outcome": rec.status,
    }
    if rec.safety_valve:
        d["safety_valve"] = True
    if rec.max_favorable_atr is not None:
        d["mfe_atr"] = rec.max_favorable_atr
    if rec.max_adverse_atr is not None:
        d["mae_atr"] = rec.max_adverse_atr
    if rec.note:
        d["human_note"] = rec.note[:400]      # 人工备注是最高信号, 但别撑爆 token
    if rec.model_note:
        d["earlier_model_note"] = rec.model_note[:400]
    detail = rec.settle_detail or {}
    if detail.get("ambiguous"):
        d["ambiguous_bar"] = True
    return d


ERROR_ANNOTATION_SYSTEM = """你是 BTC/USDT 量化系统的单笔复盘员。

系统给每笔信号打出四个面的读数 (news/data/tech/prediction, 各 ∈ [-100,+100])
与合成分 CS, 然后按事后价格结算了对错。现在这一笔判错了, 请分析**错在哪**。

必须遵守:
1. 只输出 json, 不要输出任何 json 之外的文字。
2. 指出哪一面最可能误导了判断 (若四面方向一致但都错了, 说明是系统性错配, 填 "systemic")。
3. 不要因为这一笔就建议改参数 — 这一笔只写诊断, 参数微调要等样本累积。
4. 如果信息不足以判断 (例如没记录 ATR 或窗口内数据缺失), 直说 "insufficient_info"。
5. 输出中文, 每段 1~2 句, 不要空话。"""

ERROR_ANNOTATION_SCHEMA = {
    "primary_face": "news | data | tech | prediction | systemic | insufficient_info",
    "what_went_wrong": "这笔为什么错, 1~2 句",
    "note": "给未来自己看的一句提醒, 具体可执行",
}


def build_error_annotation_messages(
    rec: TradeRecord,
    recent_stats: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """单笔错误分析的 [system, user] 消息."""
    payload: Dict[str, Any] = {
        "task": "annotate_single_error",
        "trade": _compact_trade(rec),
        "context": {
            "decision": rec.decision,
            "cs": rec.composite_score,
            "safety_valve": rec.safety_valve,
            "settle_detail": rec.settle_detail,
            "config_version": rec.config_version,
        },
        "output_schema": ERROR_ANNOTATION_SCHEMA,
    }
    if recent_stats:
        payload["recent_stats"] = recent_stats
    return [
        {"role": "system", "content": ERROR_ANNOTATION_SYSTEM},
        {"role": "user", "content":
            "请只输出 json, 字段与 output_schema 一致。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1)},
    ]


def build_review_messages(
    stats: ReviewStats,
    records: Sequence[TradeRecord],
    current_params: Dict[str, float],
    config_version: str = "",
    extra_context: str = "",
) -> List[Dict[str, str]]:
    """构造 [system, user] 两条消息."""
    errors = [r for r in records if r.status == "wrong"]
    correct = [r for r in records if r.status == "correct"]
    errors = sorted(errors, key=lambda r: r.opened_at_ms, reverse=True)[:MAX_ERRORS_IN_PAYLOAD]
    correct = sorted(correct, key=lambda r: r.opened_at_ms, reverse=True)[
        : max(0, MAX_TRADES_IN_PAYLOAD - len(errors))
    ]

    payload: Dict[str, Any] = {
        "task": "review_and_suggest_param_tuning",
        "config_version": config_version,
        "sample_note": (
            "valid = 对 + 错, 是唯一的分母。invalid (横盘两边都没触) 与 "
            "excluded (中性不操作 / 黑天鹅覆盖) 不计入胜率。"
        ),
        "stats": stats.to_dict(),
        "how_to_read_face_stats": {
            "correct / wrong / delta": (
                "各面读数的带符号均值。delta = 对组均值 − 错组均值。"
                "delta 明显偏离 0 → 这一面在某个方向上系统性偏错。"
            ),
            "abs_correct / abs_wrong / abs_delta": (
                "各面读数的**幅度**均值 (绝对值)。abs_delta = 错组幅度 − 对组幅度。"
                "abs_delta 明显为正 → 这一面读数越极端越容易判错 (不分多空), "
                "典型解法是加大该面的权重惩罚或收紧对应阈值; "
                "带符号 delta 可能因为多空相消而接近 0, 这时只有 abs_delta 能看出来。"
            ),
        },
        "current_params": {k: current_params.get(k) for k in TUNABLE_PARAMS},
        "allowed_params": {
            k: _param_spec(k, v, current_params) for k, v in TUNABLE_PARAMS.items()
        },
        "param_notes": {
            "dim_weight": "维度权重, 同组四个之和必须 = 1.0",
            "threshold": "决策阈值, 必须保持 strong_short < standard_short < watch_long < standard_long < strong_long",
            "safety_valve": "安全阀: 任一维与 CS 反向且差值超过此值则决策降级一档",
            "tech_mult": "技术面调节因子 (ADX 强弱放大/衰减, 布林挤压放大)",
        },
        "error_trades": [_compact_trade(r) for r in errors],
        "correct_trades_sample": [_compact_trade(r) for r in correct],
        "output_schema": OUTPUT_SCHEMA_HINT,
    }
    if extra_context:
        payload["extra_context"] = extra_context

    user = (
        "下面是复盘数据。请只输出 json, 字段与 output_schema 一致。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
