"""消息面因子映射器 — 短/长期双路径.

短期: breaking_crypto / whale / regulatory_event / macro_surprise / black_swan
长期: monetary_policy / etf / regulations / macro_data(含M2/PMI) / ...
缺指标记 missing + 权重重归一化兜底.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from config.mapping import (
    CPI_YOY_CHANGE_ANCHORS,
    DXY_TREND_ANCHORS,
    ETF_DAILY_FLOW_ANCHORS,
    INDPRO_YOY_ANCHORS,
    M2_YOY_ANCHORS,
    USDT_NET_MINT_ANCHORS,
    YIELD_CURVE_ANCHORS,
)
from config.weights import NEWS_SUB_WEIGHTS
from models.signals import StrategyHorizon
from models.snapshots import NewsScoreResult, NewsSnapshot
from utils.scoring import (
    available_weight_ratio,
    clamp,
    interpolate_anchors,
    renormalized_weighted_sum,
    shrink_by_confidence,
)


def _news_weight_key(horizon: StrategyHorizon) -> str:
    if horizon == StrategyHorizon.LONG_TERM:
        return "long_term"
    return "short_term_normal"


def map_halving(
    months_since: Optional[float], months_to: Optional[float]
) -> float:
    if months_since is None:
        return 0.0
    if 6 <= months_since <= 12:
        return 90.0
    if 3 <= months_since < 6:
        return 50.0
    if months_since > 18:
        return 0.0
    if months_to is not None and months_to > 12:
        return 0.0
    if 0 <= months_since < 3:
        return 20.0
    return 0.0


def map_etf(
    daily: Optional[float],
    weekly: Optional[float],
    consecutive_weeks: int,
) -> float:
    if daily is None and weekly is None:
        return 0.0
    base = 0.0
    if daily is not None:
        base = interpolate_anchors(daily, ETF_DAILY_FLOW_ANCHORS)
    if weekly is not None:
        if weekly > 1_000_000_000 and consecutive_weeks >= 3:
            return 90.0
        if weekly < -1_000_000_000:
            return -90.0
    return base


def map_institutional_from_whale(net_btc: Optional[float]) -> float:
    if net_btc is None:
        return 0.0
    if net_btc >= 5000:
        return -90.0
    if net_btc >= 1000:
        return -70.0
    if net_btc <= -1000:
        return 70.0
    if net_btc <= -5000:
        return 90.0
    return 0.0


def _uniq(missing: List[str]) -> List[str]:
    seen = set()
    out = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


class NewsFactorMapper:
    def map(
        self,
        snapshot: NewsSnapshot,
        horizon: StrategyHorizon = StrategyHorizon.SHORT_TERM,
    ) -> NewsScoreResult:
        if horizon == StrategyHorizon.LONG_TERM:
            return self._map_long(snapshot)
        return self._map_short(snapshot)

    def _map_short(self, snapshot: NewsSnapshot) -> NewsScoreResult:
        weights = NEWS_SUB_WEIGHTS["short_term_normal"]
        missing: List[str] = []
        scores: Dict[str, float] = {}

        if snapshot.breaking_sentiment is not None:
            scores["breaking_crypto"] = clamp(snapshot.breaking_sentiment)
        else:
            scores["breaking_crypto"] = 0.0
            missing.append("breaking_crypto")

        if snapshot.institutional_score is None:
            scores["whale_institutional"] = 0.0
            missing.append("whale_institutional")
        else:
            scores["whale_institutional"] = map_institutional_from_whale(
                snapshot.institutional_score
            )

        if snapshot.regulatory_event_score is not None:
            scores["regulatory_event"] = clamp(snapshot.regulatory_event_score)
        elif snapshot.regulation_score is not None:
            scores["regulatory_event"] = clamp(snapshot.regulation_score)
        else:
            scores["regulatory_event"] = 0.0
            missing.append("regulatory_event")

        if snapshot.macro_surprise_score is not None:
            scores["macro_surprise"] = clamp(snapshot.macro_surprise_score)
        elif snapshot.cpi_yoy_change is not None:
            # 发布日冲击兜底: CPI 同比变化
            scores["macro_surprise"] = interpolate_anchors(
                snapshot.cpi_yoy_change, CPI_YOY_CHANGE_ANCHORS
            )
        else:
            scores["macro_surprise"] = 0.0
            missing.append("macro_surprise")

        if snapshot.black_swan_score is not None:
            scores["black_swan"] = clamp(snapshot.black_swan_score)
        elif snapshot.black_swan_event:
            scores["black_swan"] = -80.0
        else:
            # 无极端事件 = 0 分, 不是缺项 (设计行为)
            scores["black_swan"] = 0.0

        conf = available_weight_ratio(weights, missing)
        raw = renormalized_weighted_sum(scores, weights, missing)
        s = round(clamp(shrink_by_confidence(raw, conf)), 2)
        return NewsScoreResult(
            s_news=s,
            sub_scores={k: round(v, 2) for k, v in scores.items()},
            missing_fields=_uniq(missing),
            confidence=round(conf, 3),
            reasoning=(
                f"S_news(short)={s:.1f} conf={conf:.2f} sev={snapshot.event_severity} | "
                + " ".join(f"{k}={scores[k]:.0f}" for k in scores)
            ),
        )

    def _map_long(self, snapshot: NewsSnapshot) -> NewsScoreResult:
        weights = NEWS_SUB_WEIGHTS["long_term"]
        missing: List[str] = list(snapshot.missing_fields)
        scores: Dict[str, float] = {}

        if snapshot.etf_daily_net_usd is None and snapshot.etf_weekly_net_usd is None:
            scores["etf_flows"] = 0.0
            if "etf_flows" not in missing:
                missing.append("etf_flows")
        else:
            scores["etf_flows"] = map_etf(
                snapshot.etf_daily_net_usd,
                snapshot.etf_weekly_net_usd,
                snapshot.etf_consecutive_inflow_weeks,
            )

        # 货币政策 — DXY + M2
        mon_parts: List[float] = []
        if snapshot.dxy_change_5d is not None:
            mon_parts.append(
                interpolate_anchors(snapshot.dxy_change_5d, DXY_TREND_ANCHORS)
            )
        if snapshot.m2_yoy is not None:
            mon_parts.append(interpolate_anchors(snapshot.m2_yoy, M2_YOY_ANCHORS))
        if mon_parts:
            scores["monetary_policy"] = sum(mon_parts) / len(mon_parts)
        else:
            scores["monetary_policy"] = 0.0
            if "monetary_policy" not in missing:
                missing.append("monetary_policy")

        if snapshot.regulation_score is None:
            scores["regulations"] = 0.0
            if "regulations" not in missing:
                missing.append("regulations")
        else:
            scores["regulations"] = snapshot.regulation_score

        # 经济数据 — CPI + 利差 + PMI
        macro_parts: List[float] = []
        if snapshot.cpi_yoy_change is not None:
            macro_parts.append(
                interpolate_anchors(snapshot.cpi_yoy_change, CPI_YOY_CHANGE_ANCHORS)
            )
        if snapshot.yield_curve_10y2y is not None:
            macro_parts.append(
                interpolate_anchors(snapshot.yield_curve_10y2y, YIELD_CURVE_ANCHORS)
            )
        if snapshot.pmi is not None:
            # pmi 字段 = INDPRO 同比 (免费代理)
            macro_parts.append(interpolate_anchors(snapshot.pmi, INDPRO_YOY_ANCHORS))
        if macro_parts:
            scores["macro_data"] = sum(macro_parts) / len(macro_parts)
        else:
            scores["macro_data"] = 0.0
            if "macro_data" not in missing:
                missing.append("macro_data")

        if snapshot.institutional_score is None:
            scores["institutional_gov"] = 0.0
            if "institutional_gov" not in missing:
                missing.append("institutional_gov")
        else:
            scores["institutional_gov"] = map_institutional_from_whale(
                snapshot.institutional_score
            )

        scores["halving"] = map_halving(
            snapshot.months_since_halving, snapshot.months_to_halving
        )

        if snapshot.usdt_net_mint_24h is None:
            scores["usdt_dynamics"] = 0.0
            if "usdt_dynamics" not in missing:
                missing.append("usdt_dynamics")
        else:
            scores["usdt_dynamics"] = interpolate_anchors(
                snapshot.usdt_net_mint_24h, USDT_NET_MINT_ANCHORS
            )

        if snapshot.black_swan_score is not None:
            scores["black_swan"] = clamp(snapshot.black_swan_score)
        else:
            scores["black_swan"] = 0.0
            # 有任何突发/RSS 数据时, 0 不是缺项
            if "black_swan" in missing:
                missing = [m for m in missing if m != "black_swan"]

        # 只保留长期权重相关的 missing
        missing = [m for m in missing if m in weights]

        conf = available_weight_ratio(weights, missing)
        raw = renormalized_weighted_sum(scores, weights, missing)
        s = round(clamp(shrink_by_confidence(raw, conf)), 2)
        return NewsScoreResult(
            s_news=s,
            sub_scores={k: round(v, 2) for k, v in scores.items()},
            missing_fields=_uniq(missing),
            confidence=round(conf, 3),
            reasoning=(
                f"S_news(long)={s:.1f} conf={conf:.2f} | "
                + " ".join(f"{k}={scores.get(k, 0):.0f}" for k in weights)
            ),
        )
