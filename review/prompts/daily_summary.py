"""日总结 Prompt 模板."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from models.review import TradeRecord

DAILY_SUMMARY_SYSTEM = """你是 BTC/USDT 量化交易系统的日终复盘员。

系统用四面一体框架 (消息/数据/技术/预测) 打分并自动开平仓。
你的任务: 根据当天的交易档案与统计, 产出结构化日总结。

必须遵守:
1. 只输出 json, 不要输出任何 json 之外的文字。
2. 评估各面权重是否合理, 指出哪一面权重偏高/偏低, 并给出建议目标值 (同组之和=1.0)。
3. 不要因为单笔亏损就建议大改; 关注重复出现的模式。
4. 输出中文。
"""

DAILY_SUMMARY_SCHEMA = {
    "headline": "一句话总结今天",
    "win_rate": "当天有效样本胜率, 数字或 null",
    "pnl_atr_avg": "平均 ATR 盈亏, 数字或 null",
    "face_assessment": {
        "news": {"weight_ok": "true/false", "comment": "...", "suggested_weight": "可选"},
        "data": {"weight_ok": "true/false", "comment": "...", "suggested_weight": "可选"},
        "tech": {"weight_ok": "true/false", "comment": "...", "suggested_weight": "可选"},
        "prediction": {"weight_ok": "true/false", "comment": "...", "suggested_weight": "可选"},
    },
    "patterns": ["重复出现的对/错模式, 字符串数组"],
    "risks": "今天最大风险 1~2 句",
    "tomorrow_focus": "明天最该盯的 1~2 件事",
}


def build_daily_summary_messages(
    date_str: str,
    records: Sequence[TradeRecord],
    stats: Dict[str, Any],
    current_weights: Dict[str, float],
    convergence: Dict[str, Any],
) -> List[Dict[str, str]]:
    trades = []
    for r in records:
        trades.append({
            "id": r.trade_id,
            "horizon": r.horizon,
            "decision": r.decision,
            "cs": r.composite_score,
            "faces": r.scores,
            "outcome": r.status,
            "entry": r.entry_price,
            "model_note": (r.model_note or "")[:200],
        })
    payload = {
        "task": "daily_summary",
        "date": date_str,
        "stats": stats,
        "current_dimension_weights": current_weights,
        "convergence": {
            "learning_rate": convergence.get("learning_rate"),
            "active_version": convergence.get("active_version"),
            "observation_mode": convergence.get("observation_mode"),
            "locked_params": convergence.get("locked_params"),
        },
        "trades": trades[:80],
        "output_schema": DAILY_SUMMARY_SCHEMA,
    }
    return [
        {"role": "system", "content": DAILY_SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": (
                "请只输出 json, 字段与 output_schema 一致。\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
            ),
        },
    ]
