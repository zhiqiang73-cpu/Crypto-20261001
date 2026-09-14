"""运行时：实时评分回路（免费数据源，四面齐全时输出完整 CS）。

四面都拿到值 → engine.evaluate() 真 CS + 安全阀 + 一致性放大.
任一维度缺席 → 回退 compute_partial_cs（权重重归一化，不填 0 伪装）。
黑天鹅：5min 爆仓超阈 → overridden 标记（轻量，无解除状态机）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from collectors.binance_klines import BinanceKlinesCollector
from collectors.binance_ws import BinanceFuturesCollector
from collectors.cryptopanic import CryptoPanicCollector
from collectors.defi_llama import DefiLlamaCollector
from collectors.deribit import DeribitCollector
from collectors.fear_greed import FearGreedCollector
from collectors.free_derivatives import FreeDerivativesCollector
from collectors.free_news import FreeNewsCollector
from collectors.free_onchain import FreeOnchainCollector
from collectors.fred import FredCollector
from collectors.polymarket import PolymarketCollector
from collectors.predict_fun import PredictFunCollector
from config.mapping import BLACK_SWAN_LIQ_5M_USD, SPREAD_BLACK_SWAN_MULT
from config.weights import DIMENSION_WEIGHTS
from engine.scorer import FactorScoringEngine
from indicators.engine import extract_tech_features
from mappers.data_mapper import DataFactorMapper
from mappers.news_mapper import NewsFactorMapper
from mappers.prediction_mapper import PredictionFactorMapper
from mappers.tech_mapper import TechFactorMapper
from models.signals import ActionDecision, DimensionScores, StrategyHorizon
from models.snapshots import (
    DataSnapshot,
    DataScoreResult,
    NewsScoreResult,
    PredictionSnapshot,
    PredictionScoreResult,
    TechScoreResult,
)
from utils.scoring import apply_consistency_damping, detect_session_zone, confidence_weighted_cs

logger = logging.getLogger(__name__)


@dataclass
class LiveScoreSnapshot:
    timestamp_ms: int
    mark_price: Optional[float]
    s_data: float
    s_tech: float
    s_news: Optional[float] = None
    s_prediction: Optional[float] = None
    partial_cs: float = 0.0
    composite_score: Optional[float] = None  # 四面齐全时的真 CS
    decision: ActionDecision = ActionDecision.NEUTRAL
    safety_valve: bool = False
    overridden: bool = False
    suppressed_cs: Optional[float] = None
    missing_dimensions: List[str] = field(default_factory=list)
    data_detail: Optional[DataScoreResult] = None
    tech_detail: Optional[TechScoreResult] = None
    news_detail: Optional[NewsScoreResult] = None
    prediction_detail: Optional[PredictionScoreResult] = None
    weighted_breakdown: Dict[str, float] = field(default_factory=dict)
    staleness_sec: Dict[str, Optional[float]] = field(default_factory=dict)
    reasoning: str = ""
    is_full_cs: bool = False
    # 面板监控条
    liq_5m_usd: Optional[float] = None
    spread_vs_mean: Optional[float] = None
    black_swan_liq_threshold: float = BLACK_SWAN_LIQ_5M_USD
    spread_black_swan_mult: float = SPREAD_BLACK_SWAN_MULT
    # 预测面补充展示
    predict_fun_up_prob: Optional[float] = None
    predict_fun_window: Optional[str] = None
    predict_fun_feed: Optional[str] = None
    predict_fun_1d: Optional[float] = None
    fear_greed_value: Optional[float] = None
    # V8.1 交易定仓: ATR 透出到 executor
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    atr_mean: Optional[float] = None


def _staleness(ts_ms: Optional[int], now_ms: int) -> Optional[float]:
    if ts_ms is None:
        return None
    return max(0.0, (now_ms - ts_ms) / 1000.0)


class LiveScoringLoop:
    """免费源实时评分."""

    def __init__(
        self,
        horizon: StrategyHorizon = StrategyHorizon.SHORT_TERM,
        refresh_sec: float = 15.0,
        kline_interval: str = "15m",
    ) -> None:
        self.horizon = horizon
        self.refresh_sec = refresh_sec
        self.kline_interval = kline_interval
        self.binance = BinanceFuturesCollector()
        self.free = FreeDerivativesCollector()
        self.klines = BinanceKlinesCollector()
        self.polymarket = PolymarketCollector()
        self.predict_fun = PredictFunCollector()
        self.fear_greed = FearGreedCollector()
        self.defi_llama = DefiLlamaCollector()
        self.fred = FredCollector()
        self.deribit = DeribitCollector()
        self.news = FreeNewsCollector()
        self.cryptopanic = CryptoPanicCollector()
        self.onchain = FreeOnchainCollector()
        self.data_mapper = DataFactorMapper()
        self.tech_mapper = TechFactorMapper()
        self.news_mapper = NewsFactorMapper()
        self.prediction_mapper = PredictionFactorMapper()
        self.engine = FactorScoringEngine()
        self._latest: Optional[LiveScoreSnapshot] = None
        self._running = False
        # 跨 short/long/API 共用一把锁, 避免并发 score_once 把共享采集器打挂
        self._score_lock: Optional[asyncio.Lock] = None

    @property
    def latest(self) -> Optional[LiveScoreSnapshot]:
        return self._latest

    def bind_score_lock(self, lock: asyncio.Lock) -> None:
        self._score_lock = lock

    def compute_partial_cs(
        self,
        scores: Dict[str, Optional[float]],
        confidences: Optional[Dict[str, float]] = None,
    ) -> tuple:
        """仅对非 None 维度按 DIMENSION_WEIGHTS × confidence 重归一化."""
        w_all = DIMENSION_WEIGHTS[self.horizon.value]
        missing = [k for k, v in scores.items() if v is None]
        available = {k: v for k, v in scores.items() if v is not None}
        if not available:
            return 0.0, ActionDecision.NEUTRAL, missing, "no dimensions available", {}

        conf = confidences or {}
        partial, eff = confidence_weighted_cs(available, w_all, conf)
        if not eff:
            # 全部 confidence=0 时退回等权可用面
            w_sum = sum(w_all[k] for k in available)
            if w_sum <= 0:
                return 0.0, ActionDecision.NEUTRAL, missing, "zero weight", {}
            partial = sum((w_all[k] / w_sum) * available[k] for k in available)
            eff = {k: w_all[k] / w_sum for k in available}

        partial = round(partial, 2)
        decision = self.engine._decide(partial)
        tot = sum(eff.values()) or 1.0
        breakdown = {
            k: round((eff[k] / tot) * available[k], 2) for k in available
        }
        parts = " + ".join(
            f"{k}={available[k]:.1f}×{eff[k]/tot:.2f}" for k in available
        )
        reasoning = (
            f"partial_CS={partial:.1f} [{parts}] "
            f"missing={missing} (confidence-weighted)"
        )
        return partial, decision, missing, reasoning, breakdown

    async def score_once(self) -> LiveScoreSnapshot:
        if self._score_lock is None:
            self._score_lock = asyncio.Lock()
        try:
            await asyncio.wait_for(self._score_lock.acquire(), timeout=8.0)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("score lock busy >8s") from exc
        try:
            return await self._score_once_unlocked()
        finally:
            self._score_lock.release()

    async def _score_once_unlocked(self) -> LiveScoreSnapshot:
        now_ms = int(time.time() * 1000)
        t0 = time.time()
        # REST 兜底补齐订单簿 / CVD / spread, 避免 WS 暖机期数据面大面积缺项
        try:
            bsnap = await self.binance.ensure_microstructure()
        except Exception as exc:
            logger.warning("binance ensure_microstructure: %s", exc)
            bsnap = self.binance.get_snapshot()
        logger.debug("score step binance %.1fs mark=%s", time.time() - t0, bsnap.mark_price)

        fsnap = self.free.get_snapshot()
        need_free = (
            not fsnap.available
            or fsnap.heatmap_magnet is None
            or fsnap.oi_change_5m_pct is None
        )
        if need_free:
            fsnap = await self.free.fetch_once(mark_price=bsnap.mark_price)

        oc = self.onchain.get_snapshot()
        if not oc.available:
            oc = await self.onchain.fetch_once()

        der = self.deribit.get_snapshot()
        if not der.available:
            der = await self.deribit.fetch_once(mark_price=bsnap.mark_price)

        pm = self.polymarket.get_snapshot()
        need_pm = (
            not pm.available
            or pm.btc_prob is None
            or (
                bsnap.mark_price is not None
                and pm.btc_threshold_usd is not None
                and pm.btc_threshold_usd <= bsnap.mark_price * 1.005
            )
        )
        if need_pm:
            pm = await self.polymarket.fetch_once(mark_price=bsnap.mark_price)

        pf = self.predict_fun.get_snapshot()
        need_pf = (
            not pf.available
            or pf.btc_up_prob is None
            or not (0.05 < float(pf.btc_up_prob) < 0.95)
        )
        if need_pf:
            pf = await self.predict_fun.fetch_once()

        fg = self.fear_greed.get_snapshot()
        if not fg.available:
            fg = await self.fear_greed.fetch_once()

        if not self.defi_llama.available:
            await self.defi_llama.fetch_once()
        if not self.fred.get_snapshot().available:
            await self.fred.fetch_once()
        macro = self.fred.get_snapshot()

        cp = self.cryptopanic.get_snapshot()
        if not cp.available:
            cp = await self.cryptopanic.fetch_once()

        news_raw = self.news.get_snapshot()
        usdt_net = self.defi_llama.usdt_net_24h
        if usdt_net is None and (
            oc.usdt_mint_24h is not None or oc.usdt_burn_24h is not None
        ):
            usdt_net = (oc.usdt_mint_24h or 0) - (oc.usdt_burn_24h or 0)

        if not news_raw.available:
            news_raw = await self.news.fetch_once(
                whale_institutional_score=oc.whale_net_flow_btc,
                usdt_net_mint_24h=usdt_net,
                cryptopanic_snap=cp,
            )
        else:
            if oc.whale_net_flow_btc is not None:
                news_raw.institutional_score = oc.whale_net_flow_btc
            if usdt_net is not None:
                news_raw.usdt_net_mint_24h = usdt_net
            # 热更新突发字段
            if cp.available:
                news_raw.breaking_sentiment = cp.breaking_sentiment
                news_raw.regulatory_event_score = cp.regulatory_event_score
                news_raw.macro_surprise_score = cp.macro_surprise_score
                news_raw.black_swan_score = cp.black_swan_score
                news_raw.event_severity = cp.event_severity
                news_raw.recent_headlines = list(cp.recent_headlines)
                news_raw.black_swan_event = cp.event_severity == "extreme"

        # 注入 FRED 宏观 (含 M2 / PMI)
        if macro.available:
            news_raw.cpi_yoy_change = macro.cpi_yoy_change
            news_raw.yield_curve_10y2y = macro.yield_curve_10y2y
            news_raw.fed_funds_rate = macro.fed_funds_rate
            news_raw.m2_yoy = macro.m2_yoy
            news_raw.pmi = macro.pmi
            if "macro_data" in news_raw.missing_fields and (
                macro.cpi_yoy_change is not None
                or macro.yield_curve_10y2y is not None
                or macro.pmi is not None
            ):
                news_raw.missing_fields = [
                    m for m in news_raw.missing_fields if m != "macro_data"
                ]
            if "monetary_policy" in news_raw.missing_fields and (
                news_raw.dxy_change_5d is not None or macro.m2_yoy is not None
            ):
                news_raw.missing_fields = [
                    m for m in news_raw.missing_fields if m != "monetary_policy"
                ]
        if usdt_net is not None and "usdt_dynamics" in news_raw.missing_fields:
            news_raw.missing_fields = [
                m for m in news_raw.missing_fields if m != "usdt_dynamics"
            ]

        data_snap = DataSnapshot(
            binance=bsnap,
            coinglass=fsnap,
            deribit=der,
            onchain=oc,
            session=detect_session_zone(bsnap.event_time_ms),
            timestamp_ms=bsnap.event_time_ms or now_ms,
        )
        data_result = self.data_mapper.map(data_snap, self.horizon)

        intra, daily = await self.klines.fetch_tech_inputs(self.kline_interval, 300)
        tech_snap = extract_tech_features(intra, daily)
        tech_result = self.tech_mapper.map(tech_snap, self.horizon)

        news_result = self.news_mapper.map(news_raw, self.horizon)

        pred_snap = PredictionSnapshot(
            polymarket=pm,
            predict_fun=pf,
            fear_greed=fg,
            max_pain_distance=der.max_pain_distance,
            mark_price=bsnap.mark_price,
            available=(
                pf.available
                or pm.available
                or fg.available
                or der.max_pain_distance is not None
            ),
            last_success_ts=(
                pf.last_success_ts
                or pm.last_success_ts
                or fg.last_success_ts
                or der.last_success_ts
            ),
        )
        pred_result = self.prediction_mapper.map(pred_snap, self.horizon)

        s_news: Optional[float] = news_result.s_news if news_raw.available else None
        s_pred: Optional[float] = (
            pred_result.s_prediction if pred_snap.available else None
        )
        scores = {
            "news": s_news,
            "data": data_result.s_data,
            "tech": tech_result.s_tech,
            "prediction": s_pred,
        }
        face_conf = {
            "news": float(getattr(news_result, "confidence", 1.0) or 0.0),
            "data": float(getattr(data_result, "confidence", 1.0) or 0.0),
            "tech": float(getattr(tech_result, "confidence", 1.0) or 0.0),
            "prediction": float(getattr(pred_result, "confidence", 1.0) or 0.0),
        }
        # 跨面一致性阻尼: 离群面拉向中位数 (高置信保留更多原值)
        present = {k: v for k, v in scores.items() if v is not None}
        damped = apply_consistency_damping(present, face_conf)
        for k, v in damped.items():
            scores[k] = round(v, 2)

        overridden = bool(
            fsnap.liq_total_5m_usd is not None
            and fsnap.liq_total_5m_usd >= BLACK_SWAN_LIQ_5M_USD
        ) or data_result.black_swan_liq

        is_full = all(v is not None for v in scores.values())
        safety = False
        breakdown: Dict[str, float] = {}
        composite: Optional[float] = None

        if is_full:
            dim = DimensionScores(
                news=scores["news"],  # type: ignore[arg-type]
                data=scores["data"],  # type: ignore[arg-type]
                tech=scores["tech"],  # type: ignore[arg-type]
                prediction=scores["prediction"],  # type: ignore[arg-type]
            )
            ev = self.engine.evaluate(
                self.horizon, dim, confidences=face_conf
            )
            cs = ev.composite_score
            decision = ev.decision
            safety = ev.safety_valve_triggered
            breakdown = dict(ev.weighted_breakdown)
            missing: List[str] = []
            reasoning = (
                f"CS={cs:.1f} → {decision.value} | "
                f"safety_valve={safety} | {ev.reasoning}"
            )
            composite = cs
            partial = cs
        else:
            partial, decision, missing, reasoning, breakdown = self.compute_partial_cs(
                scores, confidences=face_conf
            )
            cs = partial

        suppressed = None
        if overridden:
            suppressed = cs
            decision = ActionDecision.NEUTRAL
            reasoning = (
                f"BLACK_SWAN override: liq_5m={fsnap.liq_total_5m_usd} "
                f">= {BLACK_SWAN_LIQ_5M_USD}; suppressed_CS={suppressed}"
            )

        staleness = {
            "binance": _staleness(bsnap.event_time_ms, now_ms),
            "derivatives": _staleness(
                getattr(fsnap, "last_success_ts", None)
                if hasattr(fsnap, "last_success_ts")
                else None,
                now_ms,
            ),
            "polymarket": _staleness(pm.last_success_ts, now_ms),
            "predict_fun": _staleness(pf.last_success_ts, now_ms),
            "fear_greed": _staleness(fg.last_success_ts, now_ms),
            "deribit": _staleness(der.last_success_ts, now_ms),
            "news": _staleness(news_raw.last_success_ts, now_ms),
            "cryptopanic": _staleness(cp.last_success_ts, now_ms),
            "onchain": _staleness(oc.last_success_ts, now_ms),
            "fred": _staleness(macro.last_success_ts, now_ms),
            "defi_llama": _staleness(self.defi_llama.last_success_ts, now_ms),
        }

        out = LiveScoreSnapshot(
            # 用评分时刻, 不用 WS event_time — 否则订单簿卡住时面板 age 会假性飙升
            timestamp_ms=now_ms,
            mark_price=bsnap.mark_price,
            s_data=data_result.s_data,
            s_tech=tech_result.s_tech,
            s_news=scores["news"],
            s_prediction=scores["prediction"],
            partial_cs=partial,
            composite_score=composite,
            decision=decision,
            safety_valve=safety,
            overridden=overridden,
            suppressed_cs=suppressed,
            missing_dimensions=missing,
            data_detail=data_result,
            tech_detail=tech_result,
            news_detail=news_result,
            prediction_detail=pred_result,
            weighted_breakdown=breakdown,
            staleness_sec=staleness,
            reasoning=reasoning,
            is_full_cs=is_full and not overridden,
            liq_5m_usd=fsnap.liq_total_5m_usd,
            spread_vs_mean=bsnap.spread_vs_mean,
            black_swan_liq_threshold=BLACK_SWAN_LIQ_5M_USD,
            spread_black_swan_mult=SPREAD_BLACK_SWAN_MULT,
            predict_fun_up_prob=pf.btc_up_prob,
            predict_fun_window=pf.active_window,
            predict_fun_feed=pf.price_feed_provider,
            predict_fun_1d=pf.btc_up_prob_1d,
            fear_greed_value=fg.value,
            atr=getattr(tech_result, "atr", None) or getattr(tech_snap, "atr", None),
            atr_pct=getattr(tech_result, "atr_pct", None) or getattr(tech_snap, "atr_pct", None),
            atr_mean=None,  # 由 executor 侧滚动维护可选
        )
        self._latest = out
        return out

    async def run(self, stop_event: Optional[asyncio.Event] = None, on_score=None) -> None:
        self._running = True
        own_stop = stop_event is None
        stop = stop_event or asyncio.Event()
        # 采集器使用独立 stop, 避免某个 collector finally 误杀评分主环
        collector_stop = asyncio.Event()

        async def _propagate_stop() -> None:
            await stop.wait()
            collector_stop.set()

        prop_task = asyncio.create_task(_propagate_stop())
        tasks = [
            asyncio.create_task(self.binance.run(stop_event=collector_stop)),
            asyncio.create_task(
                self.free.run(
                    stop_event=collector_stop,
                    mark_price_provider=lambda: self.binance.snapshot.mark_price,
                )
            ),
            asyncio.create_task(
                self.polymarket.run(
                    stop_event=collector_stop,
                    mark_price_provider=lambda: self.binance.snapshot.mark_price,
                )
            ),
            asyncio.create_task(self.predict_fun.run(stop_event=collector_stop)),
            asyncio.create_task(self.fear_greed.run(stop_event=collector_stop)),
            asyncio.create_task(self.defi_llama.run(stop_event=collector_stop)),
            asyncio.create_task(self.fred.run(stop_event=collector_stop)),
            asyncio.create_task(self.cryptopanic.run(stop_event=collector_stop)),
            asyncio.create_task(
                self.deribit.run(
                    stop_event=collector_stop,
                    mark_price_provider=lambda: self.binance.snapshot.mark_price,
                )
            ),
            asyncio.create_task(self.onchain.run(stop_event=collector_stop)),
            asyncio.create_task(
                self.news.run(
                    stop_event=collector_stop,
                    onchain_provider=lambda: self.onchain.get_snapshot(),
                    cryptopanic_provider=lambda: self.cryptopanic.get_snapshot(),
                )
            ),
        ]

        await asyncio.sleep(3.0)
        logger.info("scoring loop start horizon=%s refresh=%.0fs", self.horizon.value, self.refresh_sec)
        try:
            while self._running and not stop.is_set():
                cycle_t0 = time.time()
                logger.info(
                    "score cycle begin horizon=%s stop=%s",
                    self.horizon.value,
                    stop.is_set(),
                )
                score_task = asyncio.create_task(self.score_once())
                try:
                    done, _pending = await asyncio.wait({score_task}, timeout=45.0)
                    if score_task not in done:
                        score_task.cancel()
                        logger.warning(
                            "score_once timeout (>45s) horizon=%s — abandon cycle",
                            self.horizon.value,
                        )
                        # 不 await 取消收尾: 内层若卡在 DNS/代理, wait_for 会一起死锁
                    else:
                        snap = score_task.result()
                        if on_score:
                            on_score(snap)
                        else:
                            logger.info(
                                "mark=%s S_n=%s S_d=%.1f S_t=%.1f S_p=%s CS=%s → %s full=%s pf=%s atr=%s",
                                snap.mark_price,
                                snap.s_news,
                                snap.s_data,
                                snap.s_tech,
                                snap.s_prediction,
                                snap.composite_score if snap.is_full_cs else snap.partial_cs,
                                snap.decision.value,
                                snap.is_full_cs,
                                snap.predict_fun_up_prob,
                                snap.atr,
                            )
                except asyncio.CancelledError:
                    score_task.cancel()
                    raise
                except Exception as exc:
                    logger.warning("score_once failed horizon=%s: %s", self.horizon.value, exc)
                # 刷新间隔从本轮起点算, 避免 score 慢时叠加空等
                elapsed = time.time() - cycle_t0
                wait = max(0.5, self.refresh_sec - elapsed)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            # 勿 set 父级共享 stop — 否则 short 环退出会误杀 long/scheduler
            if own_stop:
                stop.set()
            collector_stop.set()
            prop_task.cancel()
            try:
                await prop_task
            except (asyncio.CancelledError, Exception):
                pass
            self.binance.stop()
            self.free.stop()
            self.polymarket.stop()
            self.predict_fun.stop()
            self.fear_greed.stop()
            self.defi_llama.stop()
            self.fred.stop()
            self.cryptopanic.stop()
            self.deribit.stop()
            self.onchain.stop()
            self.news.stop()
            for t in tasks:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self.binance.close()
            await self.free.close()
            await self.klines.close()
            await self.polymarket.close()
            await self.predict_fun.close()
            await self.fear_greed.close()
            await self.defi_llama.close()
            await self.fred.close()
            await self.cryptopanic.close()
            await self.deribit.close()
            await self.onchain.close()
            await self.news.close()

    def stop(self) -> None:
        self._running = False
