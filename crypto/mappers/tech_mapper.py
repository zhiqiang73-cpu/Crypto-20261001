"""技术面因子映射器 — TechSnapshot → S_tech ∈ [-100, +100].

按手册第六节规则; ADX / 布林带挤压为调节因子; ATR 仅元数据。
"""

from __future__ import annotations

from typing import Dict, List

from config.weights import (
    ADX_BOOST,
    ADX_DAMPEN,
    ADX_STRONG,
    ADX_WEAK,
    BOLL_SQUEEZE_BOOST,
    TECH_INDICATOR_WEIGHTS,
)
from models.signals import StrategyHorizon
from models.snapshots import TechScoreResult, TechSnapshot
from utils.scoring import available_weight_ratio, clamp, shrink_by_confidence


class TechFactorMapper:
    def map(
        self,
        snapshot: TechSnapshot,
        horizon: StrategyHorizon = StrategyHorizon.SHORT_TERM,
        *,
        strategy_params=None,
    ) -> TechScoreResult:
        missing: List[str] = []
        if not snapshot.available:
            return TechScoreResult(
                s_tech=0.0,
                missing_fields=["tech_snapshot"],
                confidence=0.0,
                reasoning="TechSnapshot unavailable",
            )

        scores: Dict[str, float] = {
            "market_structure": snapshot.structure_score,
            "ema_stack": snapshot.ema_score,
            "volume_profile": snapshot.vp_score,
            "support_resistance": snapshot.sr_score,
            "vwap": snapshot.vwap_score,
            "pdh_pdl": snapshot.pdh_pdl_score,
            "rsi_divergence": snapshot.rsi_divergence_score,
            "macd_hist": snapshot.macd_score,
            "volume": snapshot.volume_score,
            "obv": snapshot.obv_score,
            "pin_bar": snapshot.pin_bar_score,
            "engulfing": snapshot.engulfing_score,
            "inside_bar": snapshot.inside_bar_score,
            "fibonacci": 0.0,
            "order_blocks": 0.0,
            "fvg": 0.0,
        }
        for k in ("fibonacci", "order_blocks", "fvg"):
            missing.append(k)
        if snapshot.pdh is None:
            missing.append("pdh_pdl")
        if snapshot.vp_poc is None:
            missing.append("volume_profile")
        if snapshot.ema200 is None:
            missing.append("ema_stack")

        sp = strategy_params or {}
        tech_w = sp.get("TECH_INDICATOR_WEIGHTS", TECH_INDICATOR_WEIGHTS)
        weights = tech_w[horizon.value]
        adx_strong = float(sp.get("ADX_STRONG", ADX_STRONG))
        adx_weak = float(sp.get("ADX_WEAK", ADX_WEAK))
        adx_boost = float(sp.get("ADX_BOOST", ADX_BOOST))
        adx_dampen = float(sp.get("ADX_DAMPEN", ADX_DAMPEN))
        boll_boost = float(sp.get("BOLL_SQUEEZE_BOOST", BOLL_SQUEEZE_BOOST))
        raw = 0.0
        for k, w in weights.items():
            if w == 0:
                continue
            raw += float(w) * scores.get(k, 0.0)

        adx_mult = 1.0
        if snapshot.adx is not None:
            if snapshot.adx >= adx_strong:
                adx_mult = adx_boost
            elif snapshot.adx < adx_weak:
                adx_mult = adx_dampen

        boll_mult = boll_boost if snapshot.boll_squeeze else 1.0

        conf = available_weight_ratio(weights, missing)
        # 调节只放大有方向的 raw (保留符号)
        s_tech = round(
            clamp(shrink_by_confidence(raw * adx_mult * boll_mult, conf)), 2
        )

        reasoning = (
            f"S_tech={s_tech:.1f} conf={conf:.2f} | "
            f"struct={snapshot.structure_score:.0f} "
            f"ema={snapshot.ema_score:.0f} vwap={snapshot.vwap_score:.0f} "
            f"| ADX×{adx_mult} boll×{boll_mult}"
        )
        return TechScoreResult(
            s_tech=s_tech,
            indicator_scores={k: round(v, 2) for k, v in scores.items()},
            adx_multiplier=adx_mult,
            boll_multiplier=boll_mult,
            atr=snapshot.atr,
            atr_pct=snapshot.atr_pct,
            atr_mean=getattr(snapshot, "atr_mean", None),
            missing_fields=missing,
            confidence=round(conf, 3),
            reasoning=reasoning,
        )
