"""BTC/USDT V7 双向统一评分引擎"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from typing import Dict, Optional

from config.weights import DIMENSION_WEIGHTS, DECISION_THRESHOLDS, SAFETY_VALVE_THRESHOLD
from models.signals import StrategyHorizon, ActionDecision, DimensionScores, EvaluationResult
from utils.scoring import compute_cs_with_boost

class FactorScoringEngine:
    def __init__(self, use_active_overrides: bool = False):
        # 复制一份, 避免复盘回路的 overrides 污染模块级常量
        self.weights = {k: dict(v) for k, v in DIMENSION_WEIGHTS.items()}
        self.th = dict(DECISION_THRESHOLDS)
        self.safety_valve_threshold = SAFETY_VALVE_THRESHOLD
        self.config_version = "factory"
        self.config_hash = ""
        self.config_load_ok = True
        if use_active_overrides:
            # 延迟导入: 避免 engine ← review ← engine 的循环引用
            try:
                from config.effective_config import apply_config_to_engine, freeze_effective_config
                cfg = freeze_effective_config(allow_factory_fallback=False)
                if not cfg.load_ok:
                    self.config_load_ok = False
                    import logging
                    logging.getLogger(__name__).error(
                        "生效配置无效, 交易路径应阻断: %s", cfg.load_error
                    )
                else:
                    apply_config_to_engine(self, cfg)
                    self.config_load_ok = True
            except Exception as exc:  # noqa: BLE001
                self.config_load_ok = False
                import logging
                logging.getLogger(__name__).error(
                    "加载生效配置失败, 不得静默用出厂默认继续交易: %s", exc
                )

    def apply_frozen_config(self, cfg) -> None:
        from config.effective_config import apply_config_to_engine
        apply_config_to_engine(self, cfg)
        self.config_load_ok = bool(getattr(cfg, "load_ok", True))

    def evaluate(
        self,
        horizon: StrategyHorizon,
        scores: DimensionScores,
        reasoning: str = "",
        confidences: Optional[Dict[str, float]] = None,
        *,
        weights: Optional[Dict[str, float]] = None,
        thresholds: Optional[Dict[str, float]] = None,
    ) -> EvaluationResult:
        """一次计算, 同时给出方向和强度.

        confidences: 各面可用权重占比; 参与 CS 加权并在 breakdown 中体现.
        可选 weights/thresholds 覆盖实例状态 — 必须来自本轮不可变快照。
        """
        w = weights if weights is not None else self.weights[horizon.value]
        if thresholds is not None:
            saved = self.th
            self.th = thresholds
            try:
                return self._evaluate_body(horizon, scores, reasoning, confidences, w)
            finally:
                self.th = saved
        return self._evaluate_body(horizon, scores, reasoning, confidences, w)

    def _evaluate_body(
        self,
        horizon: StrategyHorizon,
        scores: DimensionScores,
        reasoning: str,
        confidences: Optional[Dict[str, float]],
        w: Dict[str, float],
    ) -> EvaluationResult:
        face = {
            "news": scores.news,
            "data": scores.data,
            "tech": scores.tech,
            "prediction": scores.prediction,
        }
        conf_use = dict(confidences or {})
        if getattr(self, "collinear_groups", None) is not None:
            conf_use["_collinear_groups"] = self.collinear_groups
        cs, boost = compute_cs_with_boost(
            face, w, confidences=conf_use,
            enable_boost=getattr(self, "enable_agreement_boost", None),
        )
        cs = round(cs, 2)

        # breakdown: 用有效权重 (Wi×Ci) 归一后的贡献
        if confidences:
            eff = {
                k: float(w[k]) * max(0.0, min(1.0, float(confidences.get(k, 1.0))))
                for k in w
            }
            tot = sum(eff.values()) or 1.0
            bk = {
                k: round((eff[k] / tot) * float(face[k]), 2) for k in face
            }
        else:
            bk = {
                "news":       round(w["news"] * scores.news, 2),
                "data":       round(w["data"] * scores.data, 2),
                "tech":       round(w["tech"] * scores.tech, 2),
                "prediction": round(w["prediction"] * scores.prediction, 2),
            }

        # 基础决策
        decision = self._decide(cs)

        # 安全阀检查: 任一维度与CS方向矛盾超过阈值则降级
        safety_triggered = False
        for s in scores.all_scores():
            if cs > 0 and s < 0 and abs(s - cs) > self.safety_valve_threshold:
                safety_triggered = True
            elif cs < 0 and s > 0 and abs(s - cs) > self.safety_valve_threshold:
                safety_triggered = True

        if safety_triggered:
            decision = self._downgrade(decision)

        reason = reasoning or f"agreement_boost={boost:.2f}"
        if boost != 1.0 and "agreement_boost" not in reason:
            reason = f"{reason} | agreement_boost={boost:.2f}".strip(" |")

        return EvaluationResult(
            horizon=horizon, composite_score=cs, dimension_scores=scores,
            weighted_breakdown=bk, decision=decision,
            safety_valve_triggered=safety_triggered, reasoning=reason
        )

    def _decide(self, cs):
        if cs >= self.th["strong_long"]:
            return ActionDecision.STRONG_LONG
        elif cs >= self.th["standard_long"]:
            return ActionDecision.STANDARD_LONG
        elif cs >= self.th["watch_long"]:
            return ActionDecision.WATCH_LONG
        elif cs > self.th["neutral_lower"]:
            return ActionDecision.NEUTRAL
        elif cs > self.th["standard_short"]:
            return ActionDecision.WATCH_SHORT
        elif cs > self.th["strong_short"]:
            return ActionDecision.STANDARD_SHORT
        else:
            return ActionDecision.STRONG_SHORT

    def _downgrade(self, d):
        order = [
            ActionDecision.STRONG_LONG, ActionDecision.STANDARD_LONG,
            ActionDecision.WATCH_LONG, ActionDecision.NEUTRAL,
            ActionDecision.WATCH_SHORT, ActionDecision.STANDARD_SHORT,
            ActionDecision.STRONG_SHORT
        ]
        idx = order.index(d)
        # 向中性方向降一档
        if idx < 3:
            return order[min(idx + 1, 3)]
        elif idx > 3:
            return order[max(idx - 1, 3)]
        return d
