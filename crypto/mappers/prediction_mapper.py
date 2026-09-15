"""预测面因子映射器 — Predict.fun(币安) / Polymarket / F&G / Max Pain → S_prediction.

V3: Predict.fun + Polymarket 双平台为主; 币安价格源经 Predict.fun CRYPTO_UP_DOWN.
缺指标记 missing + 权重重归一化兜底.
V4: Polymarket 阈值距离校准; 面置信度 + 缺失收缩.
"""

from __future__ import annotations

from typing import Dict, List

from config.mapping import (
    FEAR_GREED_ANCHORS,
    FEDWATCH_CUT_ONLY_ANCHORS,
    FEDWATCH_HIKE_ONLY_ANCHORS,
    FEDWATCH_PROXY_ANCHORS,
    MAX_PAIN_DISTANCE_ANCHORS,
    PREDICT_FUN_UP_ANCHORS,
    PROBABILITY_CHANGE_ANCHORS,
)
from config.weights import PREDICTION_SUB_WEIGHTS
from models.signals import StrategyHorizon
from models.snapshots import PredictionScoreResult, PredictionSnapshot
from utils.scoring import (
    available_weight_ratio,
    clamp,
    interpolate_anchors,
    map_polymarket_relative,
    renormalized_weighted_sum,
    shrink_by_confidence,
)


class PredictionFactorMapper:
    def map(
        self,
        snapshot: PredictionSnapshot,
        horizon: StrategyHorizon = StrategyHorizon.SHORT_TERM,
    ) -> PredictionScoreResult:
        hkey = horizon.value
        weights = PREDICTION_SUB_WEIGHTS[hkey]
        missing: List[str] = []
        scores: Dict[str, float] = {}

        # Predict.fun：短线只用 1h 窗概率，禁止多窗混合冒充 1h
        pf = snapshot.predict_fun
        if hkey == "short_term":
            up = getattr(pf, "btc_up_prob_1h", None) if pf else None
            if up is None and pf and (getattr(pf, "active_window", None) in ("1h", "60m", "1H")):
                up = pf.btc_up_prob
        else:
            # 长期优先 1d，否则不进入方向分
            up = getattr(pf, "btc_up_prob_1d", None) if pf else None
            if up is None and pf and getattr(pf, "active_window", None) in ("1d", "24h", "1D"):
                up = pf.btc_up_prob
        if up is not None and 0.05 < up < 0.95:
            scores["predict_fun_btc"] = interpolate_anchors(
                up, PREDICT_FUN_UP_ANCHORS
            )
        else:
            scores["predict_fun_btc"] = 0.0
            missing.append("predict_fun_btc")

        # Fear & Greed (逆向指标)
        fg = snapshot.fear_greed
        if fg and fg.value is not None:
            scores["fear_greed"] = interpolate_anchors(fg.value, FEAR_GREED_ANCHORS)
        else:
            scores["fear_greed"] = 0.0
            missing.append("fear_greed")

        pm = snapshot.polymarket

        if hkey == "short_term":
            # 月度 Polymarket 事件 ≠ 1h 交易机会；无可靠期限转换前不进短线方向分
            scores["polymarket_prob"] = 0.0
            missing.append("polymarket_prob")
        elif pm.btc_prob is not None:
            # 阈值距离校准: 远离现价的低概率 ≠ 强看空
            scores["polymarket_prob"] = map_polymarket_relative(
                pm.btc_prob,
                pm.btc_threshold_usd,
                snapshot.mark_price,
            )
        else:
            scores["polymarket_prob"] = 0.0
            missing.append("polymarket_prob")

        if "prob_change_speed" in weights:
            if pm.btc_prob_change_1h is not None:
                scores["prob_change_speed"] = interpolate_anchors(
                    pm.btc_prob_change_1h, PROBABILITY_CHANGE_ANCHORS
                )
            else:
                scores["prob_change_speed"] = 0.0
                missing.append("prob_change_speed")

        # FedWatch 代理 — 映射为 0 视为无方向信息 → missing (避免虚高 conf)
        if pm.fed_cut_prob is not None and pm.fed_hike_prob is not None:
            fw = interpolate_anchors(
                pm.fed_cut_prob - pm.fed_hike_prob, FEDWATCH_PROXY_ANCHORS
            )
            if abs(fw) < 1e-9:
                scores["fedwatch_proxy"] = 0.0
                missing.append("fedwatch_proxy")
            else:
                scores["fedwatch_proxy"] = fw
        elif pm.fed_cut_prob is not None:
            fw = interpolate_anchors(pm.fed_cut_prob, FEDWATCH_CUT_ONLY_ANCHORS)
            if abs(fw) < 1e-9:
                scores["fedwatch_proxy"] = 0.0
                missing.append("fedwatch_proxy")
            else:
                scores["fedwatch_proxy"] = fw
        elif pm.fed_hike_prob is not None:
            fw = interpolate_anchors(pm.fed_hike_prob, FEDWATCH_HIKE_ONLY_ANCHORS)
            if abs(fw) < 1e-9:
                scores["fedwatch_proxy"] = 0.0
                missing.append("fedwatch_proxy")
            else:
                scores["fedwatch_proxy"] = fw
        else:
            scores["fedwatch_proxy"] = 0.0
            missing.append("fedwatch_proxy")

        dist = snapshot.max_pain_distance
        if dist is not None:
            scores["max_pain"] = interpolate_anchors(
                dist, MAX_PAIN_DISTANCE_ANCHORS
            )
        else:
            scores["max_pain"] = 0.0
            missing.append("max_pain")

        conf = available_weight_ratio(weights, missing)
        raw = renormalized_weighted_sum(scores, weights, missing)
        s = round(clamp(shrink_by_confidence(raw, conf)), 2)

        return PredictionScoreResult(
            s_prediction=s,
            sub_scores={k: round(v, 2) for k, v in scores.items()},
            missing_fields=missing,
            confidence=round(conf, 3),
            reasoning=(
                f"S_pred={s:.1f} conf={conf:.2f} | "
                + " ".join(f"{k}={scores[k]:.0f}" for k in scores)
            ),
        )
