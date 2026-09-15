"""数据面因子映射器 — 原始读数 → [-100, +100] 的 S_data.

严格遵循 btc_factor_classification.md 第五节规则。
缺指标记 0, 不重归一化; 只输出 DataScoreResult, 不伪造完整 CS。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from config.mapping import (
    BID_ASK_RATIO_ANCHORS,
    BLACK_SWAN_LIQ_5M_USD,
    CVD_5M_ANCHORS,
    EXCHANGE_RESERVES_PROXY_ANCHORS,
    FUNDING_EXTREME_ANNUAL_PCT,
    FUNDING_NORMAL_ANNUAL_PCT,
    FUNDING_RATE_ANCHORS,
    HASHRATE_CHANGE_ANCHORS,
    HEATMAP_MAGNET_ANCHORS,
    IV_ANCHORS,
    LIQUIDATION_5M_USD,
    LIQ_REALTIME_USD_ANCHORS,
    LIQ_SPEED_CLEARED_ANCHORS,
    LONG_SHORT_RATIO_ANCHORS,
    MAX_PAIN_DISTANCE_ANCHORS,
    MVRV_ANCHORS,
    OI_CHANGE_5M_ANCHORS,
    OI_DROP_5M_PCT,
    OI_EXTREME_BUILD_24H_PCT,
    SESSION_ASIA_MULTIPLIER,
    SESSION_US_MULTIPLIER,
    SPREAD_BLACK_SWAN_MULT,
    SPREAD_DANGER_MULT,
    WHALE_EXCHANGE_BTC_THRESHOLD,
    WHALE_NET_BTC_ANCHORS,
)
from config.weights import (
    DATA_LAYER_WEIGHTS,
    DERIVATIVES_INDICATOR_WEIGHTS,
    MICROSTRUCTURE_INDICATOR_WEIGHTS,
    ONCHAIN_INDICATOR_WEIGHTS,
)
from models.signals import StrategyHorizon
from models.snapshots import (
    BlockTradeBias,
    DataScoreResult,
    DataSnapshot,
    SessionZone,
)
from utils.scoring import (
    available_weight_ratio,
    clamp,
    interpolate_anchors,
    renormalized_weighted_sum,
    shrink_by_confidence,
)


# 大单状态 → 分数 (手册 5.3)
BLOCK_TRADE_SCORES = {
    BlockTradeBias.NEUTRAL: 0.0,
    BlockTradeBias.ACCUMULATION: 70.0,    # 连续大买 + 价格不跌
    BlockTradeBias.BUY_FOLLOW: 50.0,      # 大买 + 价格涨
    BlockTradeBias.SELL_ABSORBED: 40.0,   # 连续大卖 + 价格不跌
    BlockTradeBias.DISTRIBUTION: -60.0,   # 大卖 + 价格跌
}


class DataFactorMapper:
    """将 DataSnapshot 映射为数据面综合分 S_data."""

    def map(
        self,
        snapshot: DataSnapshot,
        horizon: StrategyHorizon = StrategyHorizon.SHORT_TERM,
    ) -> DataScoreResult:
        hkey = horizon.value
        missing: List[str] = []
        scores: Dict[str, float] = {}

        bn = snapshot.binance
        cg = snapshot.coinglass

        # ----- 衍生品子指标 -----
        scores["funding_rate"] = self._map_funding(
            bn.funding_rate_annualized, missing
        )
        scores["liquidation_heatmap"] = self._map_heatmap(
            cg.heatmap_magnet, missing
        )
        scores["open_interest"] = self._map_open_interest(
            cg.oi_change_5m_pct,
            cg.oi_change_24h_pct,
            bn.funding_rate_annualized,
            missing,
        )
        scores["cvd"] = self._map_cvd(bn.cvd_5m_usd, missing)

        scores["liquidations_realtime"] = self._map_realtime_liq(
            cg.liq_long_5m_usd, cg.liq_short_5m_usd, missing,
            window_status=getattr(cg, "liq_window_status", None),
        )
        scores["long_short_ratio"] = self._map_lsr(cg.long_short_ratio, missing)

        der = snapshot.deribit
        if der and der.max_pain_distance is not None:
            scores["option_max_pain"] = interpolate_anchors(
                der.max_pain_distance, MAX_PAIN_DISTANCE_ANCHORS
            )
        else:
            scores["option_max_pain"] = 0.0
            missing.append("option_max_pain")

        if der and der.iv is not None:
            scores["implied_volatility"] = interpolate_anchors(
                der.iv, IV_ANCHORS
            )
        else:
            scores["implied_volatility"] = 0.0
            missing.append("implied_volatility")

        # ----- 微观结构子指标 -----
        scores["orderbook_depth"] = self._map_orderbook(
            bn.bid_ask_ratio_2pct, missing
        )
        scores["block_trades"] = BLOCK_TRADE_SCORES.get(
            bn.block_trade_bias, 0.0
        )
        scores["spread"], spread_danger = self._map_spread(
            bn.spread_vs_mean, missing
        )
        scores["oi_velocity"] = self._map_oi_velocity(
            cg.oi_change_5m_pct,
            cg.liq_long_5m_usd,
            cg.liq_short_5m_usd,
            missing,
        )
        scores["liquidation_speed"] = self._map_liquidation_speed(
            cg.liquidation_speed_long_cleared,
            cg.liquidation_speed_short_cleared,
            missing,
        )
        scores["session_liquidity"] = 0.0  # 调节因子, 不直接出方向分

        # ----- 链上: hashrate / whale / mvrv~ / exchange_reserves~ -----
        oc = snapshot.onchain
        scores["hashrate"] = self._map_hashrate(
            oc.hashrate_ma30_change_pct if oc else None, missing
        )
        scores["whale_transfers"] = self._map_whale(
            oc.whale_net_flow_btc if oc else None,
            oc.whale_transfer_direction if oc else None,
            missing,
        )
        # 无真实 MVRV：不把年均价送入 MVRV_ANCHORS；mvrv 交易分恒缺失
        scores["mvrv"] = 0.0
        missing.append("mvrv")
        # 24h 动量：独立键 price_momentum_24h，不再冒充 exchange_reserves
        mom_val = None
        if oc is not None:
            mom_val = getattr(oc, "price_momentum_24h", None)
            if mom_val is None:
                mom_val = getattr(oc, "exchange_reserves_proxy", None)
        if mom_val is not None:
            scores["price_momentum_24h"] = interpolate_anchors(
                mom_val, EXCHANGE_RESERVES_PROXY_ANCHORS
            )
        else:
            scores["price_momentum_24h"] = 0.0
            missing.append("price_momentum_24h")
        # 旧键置零，避免储备语义泄漏
        scores["exchange_reserves"] = 0.0
        missing.append("exchange_reserves")

        onchain_keys = list(ONCHAIN_INDICATOR_WEIGHTS[hkey].keys())
        for k in onchain_keys:
            if k not in scores:
                scores[k] = 0.0
                # 仅对权重>0 的缺项计入 missing (零权重永久缺源不拖 conf)
                if ONCHAIN_INDICATOR_WEIGHTS[hkey].get(k, 0) > 0:
                    missing.append(k)

        # ----- 时区调节: 方向性指标 × multiplier -----
        session_mult = self._session_multiplier(snapshot.session)
        directional_keys = [
            "funding_rate", "liquidation_heatmap", "open_interest",
            "liquidations_realtime", "long_short_ratio",
            "orderbook_depth", "block_trades", "oi_velocity",
            "liquidation_speed", "cvd",
        ]
        if session_mult != 1.0:
            for k in directional_keys:
                if k in scores:
                    scores[k] = clamp(scores[k] * session_mult)

        # ----- 子层加权 (缺项权重重归一化) -----
        layer_w = DATA_LAYER_WEIGHTS[hkey]
        onchain_s = renormalized_weighted_sum(
            scores, ONCHAIN_INDICATOR_WEIGHTS[hkey], missing
        )
        deriv_s = renormalized_weighted_sum(
            scores, DERIVATIVES_INDICATOR_WEIGHTS[hkey], missing
        )
        micro_s = renormalized_weighted_sum(
            scores, MICROSTRUCTURE_INDICATOR_WEIGHTS[hkey], missing
        )

        # 全局可用权重占比 (层权重 × 子指标权重)
        all_w: Dict[str, float] = {}
        for k, w in ONCHAIN_INDICATOR_WEIGHTS[hkey].items():
            all_w[k] = layer_w["onchain"] * float(w)
        for k, w in DERIVATIVES_INDICATOR_WEIGHTS[hkey].items():
            all_w[k] = layer_w["derivatives"] * float(w)
        for k, w in MICROSTRUCTURE_INDICATOR_WEIGHTS[hkey].items():
            all_w[k] = layer_w["microstructure"] * float(w)
        conf = available_weight_ratio(all_w, missing)

        raw_data = (
            layer_w["onchain"] * onchain_s
            + layer_w["derivatives"] * deriv_s
            + layer_w["microstructure"] * micro_s
        )
        s_data = round(clamp(shrink_by_confidence(raw_data, conf)), 2)

        black_swan = (
            cg.liq_total_5m_usd is not None
            and cg.liq_total_5m_usd >= BLACK_SWAN_LIQ_5M_USD
        )

        # 去重 missing 但保持可读
        seen = set()
        missing_unique = []
        for m in missing:
            if m not in seen:
                seen.add(m)
                missing_unique.append(m)

        reasoning = (
            f"S_data={s_data:.1f} conf={conf:.2f} | onchain={onchain_s:.1f} "
            f"deriv={deriv_s:.1f} micro={micro_s:.1f} | "
            f"session×{session_mult}"
        )

        return DataScoreResult(
            s_data=s_data,
            onchain_score=round(onchain_s, 2),
            derivatives_score=round(deriv_s, 2),
            microstructure_score=round(micro_s, 2),
            indicator_scores={k: round(v, 2) for k, v in scores.items()},
            layer_scores={
                "onchain": round(onchain_s, 2),
                "derivatives": round(deriv_s, 2),
                "microstructure": round(micro_s, 2),
            },
            missing_fields=missing_unique,
            spread_danger=spread_danger,
            black_swan_liq=black_swan,
            session_multiplier=session_mult,
            confidence=round(conf, 3),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # 单项映射
    # ------------------------------------------------------------------

    def _map_funding(
        self, annualized: Optional[float], missing: List[str]
    ) -> float:
        if annualized is None:
            missing.append("funding_rate")
            return 0.0
        return interpolate_anchors(annualized, FUNDING_RATE_ANCHORS)

    def _map_cvd(self, cvd_usd: Optional[float], missing: List[str]) -> float:
        if cvd_usd is None:
            missing.append("cvd")
            return 0.0
        return interpolate_anchors(cvd_usd, CVD_5M_ANCHORS)

    def _map_heatmap(
        self, magnet: Optional[float], missing: List[str]
    ) -> float:
        if magnet is None:
            missing.append("liquidation_heatmap")
            return 0.0
        return interpolate_anchors(magnet, HEATMAP_MAGNET_ANCHORS)

    def _map_open_interest(
        self,
        ch_5m: Optional[float],
        ch_24h: Optional[float],
        funding_annual: Optional[float],
        missing: List[str],
    ) -> float:
        """OI 连续映射 + 费率方向符号.

        - 5m 变化用 OI_CHANGE_5M_ANCHORS (骤降→正分=出清偏多)
        - 若费率极端偏正(多头拥挤), 出清分翻空; 偏负则保持偏多
        - 24h 极端堆积叠加费率方向的小幅分
        """
        if ch_5m is None and ch_24h is None:
            missing.append("open_interest")
            return 0.0

        fr = funding_annual if funding_annual is not None else 0.0
        # funding_sign: 负费率(空头拥挤)出清后偏多 → +1
        funding_sign = 1.0 if fr < 0 else (-1.0 if fr > 0 else 1.0)

        score = 0.0
        if ch_5m is not None:
            raw = interpolate_anchors(ch_5m, OI_CHANGE_5M_ANCHORS)
            # 骤降区 (出清): 用费率决定方向; 堆积区保持锚点符号再乘费率
            if ch_5m <= -OI_DROP_5M_PCT:
                score = abs(raw) * funding_sign
            else:
                score = raw * (funding_sign if abs(fr) > FUNDING_NORMAL_ANNUAL_PCT else 1.0)

        if ch_24h is not None and ch_24h >= OI_EXTREME_BUILD_24H_PCT:
            build = interpolate_anchors(ch_24h, [
                (0.05, -10.0), (0.15, -30.0), (0.30, -50.0),
            ])
            score = clamp(score + build * funding_sign, -100, 100)

        return round(score, 2)

    def _map_realtime_liq(
        self,
        long_usd: Optional[float],
        short_usd: Optional[float],
        missing: List[str],
        window_status: Optional[str] = None,
    ) -> float:
        """区分真零 / 暖机 / 断流 / 失败。有价格不等于清算成功。"""
        status = (window_status or "").lower() or None
        if long_usd is None and short_usd is None:
            missing.append("liquidations_realtime")
            return 0.0
        if status in ("warmup", "stale", "error", "partial", "missing"):
            missing.append(f"liquidations_realtime:{status}")
            return 0.0
        if status is None and (long_usd is None or short_usd is None):
            # 单侧缺失 = 部分样本，不当真零
            missing.append("liquidations_realtime:partial")
            return 0.0
        long_usd = 0.0 if long_usd is None else float(long_usd)
        short_usd = 0.0 if short_usd is None else float(short_usd)
        if long_usd >= short_usd and long_usd > 0:
            return round(interpolate_anchors(long_usd, LIQ_REALTIME_USD_ANCHORS), 2)
        if short_usd > long_usd and short_usd > 0:
            return round(-interpolate_anchors(short_usd, LIQ_REALTIME_USD_ANCHORS), 2)
        # 窗口完整真零：分=0 且不记 missing
        return 0.0

    def _map_lsr(self, ratio: Optional[float], missing: List[str]) -> float:
        if ratio is None:
            missing.append("long_short_ratio")
            return 0.0
        return interpolate_anchors(ratio, LONG_SHORT_RATIO_ANCHORS)

    def _map_orderbook(
        self, ratio: Optional[float], missing: List[str]
    ) -> float:
        if ratio is None:
            missing.append("orderbook_depth")
            return 0.0
        return interpolate_anchors(ratio, BID_ASK_RATIO_ANCHORS)

    def _map_spread(
        self, vs_mean: Optional[float], missing: List[str]
    ) -> Tuple[float, bool]:
        """Spread 不提供方向; >3x → 0; >5x → danger 标记."""
        if vs_mean is None:
            missing.append("spread")
            return 0.0, False
        danger = vs_mean >= SPREAD_BLACK_SWAN_MULT
        if vs_mean >= SPREAD_DANGER_MULT:
            return 0.0, danger
        return 0.0, danger

    def _map_oi_velocity(
        self,
        ch_5m: Optional[float],
        long_liq: Optional[float],
        short_liq: Optional[float],
        missing: List[str],
    ) -> float:
        """OI 变化速度连续映射; 方向由爆仓主导."""
        if ch_5m is None:
            missing.append("oi_velocity")
            return 0.0
        magnitude = abs(interpolate_anchors(ch_5m, OI_CHANGE_5M_ANCHORS))
        if ch_5m > -OI_DROP_5M_PCT * 0.5:
            return round(magnitude * 0.3 if ch_5m < 0 else 0.0, 2)
        long_liq = long_liq or 0.0
        short_liq = short_liq or 0.0
        if long_liq > short_liq and long_liq > 0:
            return round(magnitude, 2)
        if short_liq > long_liq and short_liq > 0:
            return round(-magnitude, 2)
        return round(magnitude * 0.5, 2)

    def _map_liquidation_speed(
        self,
        long_cleared: Optional[float],
        short_cleared: Optional[float],
        missing: List[str],
    ) -> float:
        """清算速度连续映射: 清除比例 → 分."""
        if long_cleared is None and short_cleared is None:
            missing.append("liquidation_speed")
            return 0.0
        if long_cleared is not None and (
            short_cleared is None or long_cleared >= short_cleared
        ):
            return round(interpolate_anchors(long_cleared, LIQ_SPEED_CLEARED_ANCHORS), 2)
        if short_cleared is not None:
            return round(-interpolate_anchors(short_cleared, LIQ_SPEED_CLEARED_ANCHORS), 2)
        return 0.0

    def _map_hashrate(
        self, change_pct: Optional[float], missing: List[str]
    ) -> float:
        if change_pct is None:
            missing.append("hashrate")
            return 0.0
        return interpolate_anchors(change_pct, HASHRATE_CHANGE_ANCHORS)

    def _map_whale(
        self,
        net_btc: Optional[float],
        direction: Optional[str],
        missing: List[str],
    ) -> float:
        """巨鲸净流入连续映射; direction 无数值时作兜底."""
        if net_btc is None and not direction:
            missing.append("whale_transfers")
            return 0.0
        if net_btc is not None:
            return round(interpolate_anchors(net_btc, WHALE_NET_BTC_ANCHORS), 2)
        if direction == "to_exchange":
            return -70.0
        if direction == "from_exchange":
            return 70.0
        return 0.0

    @staticmethod
    def _session_multiplier(session: SessionZone) -> float:
        if session == SessionZone.ASIA:
            return SESSION_ASIA_MULTIPLIER
        return SESSION_US_MULTIPLIER

    @staticmethod
    def _weighted_sum(scores: Dict[str, float], weights: Dict[str, float]) -> float:
        total_w = sum(weights.values())
        if total_w <= 0:
            return 0.0
        acc = 0.0
        for k, w in weights.items():
            if w == 0:
                continue
            acc += w * scores.get(k, 0.0)
        return acc
