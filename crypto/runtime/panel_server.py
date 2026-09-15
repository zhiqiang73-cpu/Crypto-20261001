"""本机面板后端 — 服务 btc-four-face-monitor.html + /api/live.

只监听 127.0.0.1 (config.review.PANEL_HOST/PORT).
驱动行 note 为模板自动生成 (v1 简化).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

from config.mapping import (
    COINGLASS_POLL_INTERVAL_SEC,
    DERIBIT_POLL_SEC,
    NEWS_POLL_SEC,
    POLYMARKET_POLL_SEC,
    PREDICT_FUN_POLL_SEC,
)
from config.review import PANEL_HOST, PANEL_HTML, PANEL_PORT, RISK_PER_TRADE_PCT
from config.weights import (
    DECISION_THRESHOLDS,
    DIMENSION_WEIGHTS,
    NEWS_SUB_WEIGHTS,
    PREDICTION_SUB_WEIGHTS,
    TECH_INDICATOR_WEIGHTS,
)
from models.signals import ActionDecision, StrategyHorizon
from runtime.live_loop import LiveScoreSnapshot, LiveScoringLoop

logger = logging.getLogger(__name__)

# 各面「采集轮询」标称周期 (秒) — 圆环按此倒计时; 评分合成另有节拍
FACE_REFRESH_SEC = {
    "消息面": NEWS_POLL_SEC,  # 30s
    "数据面": float(COINGLASS_POLL_INTERVAL_SEC),  # 衍生品 REST; 盘口本身是 WS
    "技术面": 10.0,  # 随 score_once / K 线 (短期 CS 10s)
    "预测面": max(POLYMARKET_POLL_SEC, DERIBIT_POLL_SEC, PREDICT_FUN_POLL_SEC),
}

TIER_LABELS = {
    ActionDecision.STRONG_LONG: ("强力做多", "long"),
    ActionDecision.STANDARD_LONG: ("标准做多", "long"),
    ActionDecision.WATCH_LONG: ("偏多观望", "flat"),
    ActionDecision.NEUTRAL: ("中性", "flat"),
    ActionDecision.WATCH_SHORT: ("偏空观望", "flat"),
    ActionDecision.STANDARD_SHORT: ("标准做空", "short"),
    ActionDecision.STRONG_SHORT: ("强力做空", "short"),
}

# 驱动行中文名 (前端展示)
DRIVER_LABELS = {
    "monetary_policy": "货币政策",
    "etf_flows": "ETF 资金通道",
    "regulations": "监管政策",
    "macro_data": "经济数据",
    "institutional_gov": "机构 / 政府",
    "halving": "减半周期",
    "usdt_dynamics": "USDT 动态",
    "black_swan": "地缘 / 黑天鹅",
    "breaking_crypto": "加密突发",
    "whale_institutional": "巨鲸异动",
    "regulatory_event": "监管事件",
    "macro_surprise": "宏观意外",
    "funding_rate": "资金费率",
    "liquidation_heatmap": "清算热力图",
    "open_interest": "聚合 OI",
    "cvd": "CVD",
    "liquidations_realtime": "实时爆仓",
    "long_short_ratio": "多空比",
    "option_max_pain": "期权引力",
    "implied_volatility": "IV",
    "orderbook_depth": "订单簿深度",
    "block_trades": "大额成交",
    "spread": "Spread",
    "oi_velocity": "OI 变化速度",
    "liquidation_speed": "清算速度",
    "session_liquidity": "时区流动性",
    "hashrate": "算力",
    "whale_transfers": "巨鲸异动",
    "exchange_reserves": "24h动量(非储备)~",
    "price_momentum_24h": "24h价格动量",
    "mvrv": "MVRV~",
    "lth_supply_ratio": "LTH 持仓占比",
    "nupl": "NUPL",
    "miner_reserves": "矿工持仓",
    "usdt_market_cap": "USDT 总市值",
    "sopr": "SOPR",
    "market_structure": "市场结构",
    "ema_stack": "EMA 21/50/200",
    "volume_profile": "Volume Profile",
    "support_resistance": "支撑 / 阻力",
    "vwap": "VWAP",
    "pdh_pdl": "PDH / PDL",
    "rsi_divergence": "RSI 背离",
    "macd_hist": "MACD 柱",
    "volume": "成交量",
    "obv": "OBV",
    "pin_bar": "Pin Bar",
    "engulfing": "吞没",
    "inside_bar": "Inside Bar",
    "predict_fun_btc": "币安预测 (Predict.fun)",
    "fear_greed": "恐惧贪婪指数",
    "polymarket_prob": "Polymarket 月度方向",
    "prob_change_speed": "概率变化速度",
    "fedwatch_proxy": "FedWatch 代理",
    "max_pain": "期权引力方向",
}


def _fmt_note(name: str, val: float, wt: str) -> str:
    label = DRIVER_LABELS.get(name, name)
    return f"{label} · 现读数 {val:+.0f} · 权重 {wt}"


def _refresh_meta(
    interval_sec: float,
    age_sec: Optional[float],
) -> Dict[str, Any]:
    """圆环: remain = interval - age (夹在 0..interval)."""
    iv = float(interval_sec)
    age = float(age_sec) if age_sec is not None else 0.0
    remain = max(0.0, iv - (age % iv if iv > 0 else 0.0))
    return {
        "interval_sec": round(iv, 1),
        "age_sec": round(age, 1) if age_sec is not None else None,
        "remain_sec": round(remain, 1),
        "pct": round(remain / iv, 4) if iv > 0 else 0.0,
    }


def _face_age(staleness: Dict[str, Optional[float]], *keys: str) -> Optional[float]:
    ages = [staleness.get(k) for k in keys if staleness.get(k) is not None]
    if not ages:
        return None
    return max(ages)  # type: ignore[arg-type]


def _fmt_yi(usd: Optional[float]) -> str:
    """USD → 「x.x 亿」."""
    if usd is None:
        return "—"
    return f"{usd / 1e8:.1f} 亿"


def _drivers_from_scores(
    scores: Dict[str, float],
    weights: Dict[str, float],
    top_n: int = 5,
    col_pct: bool = True,
) -> List[dict]:
    items = []
    for k, v in scores.items():
        w = weights.get(k, 0)
        if w == 0 and abs(v) < 1e-9:
            continue
        items.append((abs(v) * (w or 0.01), k, v, w))
    items.sort(reverse=True)
    out = []
    for _, k, v, w in items[:top_n]:
        # 权重一律为 [0,1] 小数; col_pct=False 时也按百分比展示,
        # 避免 0.18 → f"{w:.0f}" 显示成「0」的漏洞
        if abs(float(w)) <= 1.0 + 1e-9:
            wt = f"{float(w)*100:.0f}%"
        else:
            wt = f"{float(w):.0f}" if not col_pct else f"{float(w):.0f}%"
        out.append({
            "wt": wt,
            "name": DRIVER_LABELS.get(k, k),
            "val": round(v, 1),
            "note": _fmt_note(k, v, wt),
            "key": k,
        })
    return out


def snapshot_to_panel(
    snap: LiveScoreSnapshot,
    horizon: StrategyHorizon,
) -> Dict[str, Any]:
    """对齐 HTML 里 TF.short / TF.long 结构."""
    hkey = horizon.value
    w = DIMENSION_WEIGHTS[hkey]
    cs = snap.composite_score if snap.is_full_cs else snap.partial_cs
    if snap.overridden and snap.suppressed_cs is not None:
        cs_display = snap.suppressed_cs
    else:
        cs_display = cs

    tier, tone = TIER_LABELS.get(snap.decision, ("中性", "flat"))
    if snap.overridden:
        tier, tone = "黑天鹅覆盖", "short"

    # 贡献 = W_i * S_i (齐全时用 breakdown; 否则按可用面)
    s_news = snap.s_news if snap.s_news is not None else 0.0
    s_pred = snap.s_prediction if snap.s_prediction is not None else 0.0
    if snap.weighted_breakdown:
        contrib = [
            snap.weighted_breakdown.get("news", round(w["news"] * s_news, 2)),
            snap.weighted_breakdown.get("data", round(w["data"] * snap.s_data, 2)),
            snap.weighted_breakdown.get("tech", round(w["tech"] * snap.s_tech, 2)),
            snap.weighted_breakdown.get(
                "prediction", round(w["prediction"] * s_pred, 2)
            ),
        ]
    else:
        contrib = [
            round(w["news"] * s_news, 2),
            round(w["data"] * snap.s_data, 2),
            round(w["tech"] * snap.s_tech, 2),
            round(w["prediction"] * s_pred, 2),
        ]

    news_w = NEWS_SUB_WEIGHTS[
        "long_term" if horizon == StrategyHorizon.LONG_TERM else "short_term_normal"
    ]
    tech_w = TECH_INDICATOR_WEIGHTS[hkey]
    pred_w = PREDICTION_SUB_WEIGHTS[hkey]

    news_scores = (snap.news_detail.sub_scores if snap.news_detail else {}) or {}
    data_scores = (snap.data_detail.indicator_scores if snap.data_detail else {}) or {}
    tech_scores = (snap.tech_detail.indicator_scores if snap.tech_detail else {}) or {}
    pred_scores = (
        snap.prediction_detail.sub_scores if snap.prediction_detail else {}
    ) or {}

    # 数据面用全局有效权重 = 层权重 × 子指标权重
    from config.weights import (
        DATA_LAYER_WEIGHTS,
        DERIVATIVES_INDICATOR_WEIGHTS,
        MICROSTRUCTURE_INDICATOR_WEIGHTS,
        ONCHAIN_INDICATOR_WEIGHTS,
    )
    layer_w = DATA_LAYER_WEIGHTS[hkey]
    data_w = {
        **{
            k: layer_w["onchain"] * float(v)
            for k, v in ONCHAIN_INDICATOR_WEIGHTS[hkey].items()
        },
        **{
            k: layer_w["derivatives"] * float(v)
            for k, v in DERIVATIVES_INDICATOR_WEIGHTS[hkey].items()
        },
        **{
            k: layer_w["microstructure"] * float(v)
            for k, v in MICROSTRUCTURE_INDICATOR_WEIGHTS[hkey].items()
        },
    }

    import time as _time
    stale = snap.staleness_sec or {}
    score_age = None
    if snap.timestamp_ms:
        score_age = max(0.0, (_time.time() * 1000 - snap.timestamp_ms) / 1000.0)

    pred_drivers = _drivers_from_scores(pred_scores, pred_w)
    for d in pred_drivers:
        if d.get("key") == "predict_fun_btc":
            up = getattr(snap, "predict_fun_up_prob", None)
            win = getattr(snap, "predict_fun_window", None) or "?"
            feed = getattr(snap, "predict_fun_feed", None) or "BINANCE"
            if up is not None:
                d["note"] = (
                    f"{feed}·Predict.fun {win} Up={up*100:.0f}% · "
                    f"映射分 {d.get('val', 0):+.0f} · 权重 {d.get('wt', '')}"
                )
            else:
                d["note"] = (
                    f"{d['note']} · 币安价格源 CRYPTO_UP_DOWN 活盘共识"
                )
        elif d.get("key") == "fear_greed":
            fg = getattr(snap, "fear_greed_value", None)
            if fg is not None:
                d["note"] = (
                    f"Fear&Greed={fg:.0f} · 逆向映射分 {d.get('val', 0):+.0f}"
                )
        elif d.get("key") == "polymarket_prob":
            d["note"] = (
                f"{d['note']} · Polymarket 月度阈值（与 Predict.fun 双平台）"
            )
        elif d.get("key") == "fedwatch_proxy":
            d["note"] = (
                f"{d['note']} · FedWatch 代理；仅有降息合约时「不降息≠加息」"
            )
        elif d.get("key") == "max_pain":
            d["note"] = (
                f"{d['note']} · 期权到期引力方向（价低于 Max Pain → 偏多）"
            )
    pred_foot = (
        ("缺项: " + ", ".join(snap.prediction_detail.missing_fields))
        if snap.prediction_detail and snap.prediction_detail.missing_fields
        else "Predict.fun(币安) + Polymarket 双平台为主"
    )

    faces = [
        {
            "name": "消息面",
            "w": f"{w['news']*100:.0f}%",
            "s": round(s_news, 1) if snap.s_news is not None else 0.0,
            "col1": "权重",
            "col2": "子类",
            "foot": (
                ("缺项: " + ", ".join(snap.news_detail.missing_fields[:4]))
                if snap.news_detail and snap.news_detail.missing_fields
                else "实时读数"
            ),
            "drivers": _drivers_from_scores(news_scores, news_w),
            "refresh": _refresh_meta(
                FACE_REFRESH_SEC["消息面"],
                _face_age(stale, "news", "cryptopanic", "onchain", "fred"),
            ),
        },
        {
            "name": "数据面",
            "w": f"{w['data']*100:.0f}%",
            "s": round(snap.s_data, 1),
            "col1": "权重",
            "col2": "指标",
            "foot": (
                ("缺项: " + ", ".join(snap.data_detail.missing_fields[:4]))
                if snap.data_detail and snap.data_detail.missing_fields
                else "实时读数"
            ),
            "drivers": _drivers_from_scores(data_scores, data_w),
            "refresh": _refresh_meta(
                FACE_REFRESH_SEC["数据面"],
                _face_age(stale, "binance", "derivatives") or _face_age(stale, "binance"),
            ),
        },
        {
            "name": "技术面",
            "w": f"{w['tech']*100:.0f}%",
            "s": round(snap.s_tech, 1),
            "col1": "权重",
            "col2": "工具",
            "foot": (
                (snap.tech_detail.reasoning[:80] if snap.tech_detail else "—")
            ),
            "drivers": _drivers_from_scores(tech_scores, tech_w),
            "refresh": _refresh_meta(
                FACE_REFRESH_SEC["技术面"],
                score_age,
            ),
        },
        {
            "name": "预测面",
            "w": f"{w['prediction']*100:.0f}%",
            "s": round(s_pred, 1) if snap.s_prediction is not None else 0.0,
            "col1": "—",
            "col2": "指标",
            "foot": pred_foot,
            "drivers": pred_drivers,
            "refresh": _refresh_meta(
                FACE_REFRESH_SEC["预测面"],
                _face_age(stale, "polymarket", "deribit"),
            ),
        },
    ]

    valve_ok = not snap.safety_valve
    valve_txt = (
        "安全阀未触发"
        if valve_ok
        else "安全阀已触发 · 决策已降档"
    )
    if snap.missing_dimensions:
        valve_txt += f" · 缺面 {snap.missing_dimensions}"

    tf_tag = (
        "长期 · 月级持仓"
        if horizon == StrategyHorizon.LONG_TERM
        else "短期 · 1小时"
    )
    note = (
        f"口径 <b>{'长期（月级持仓）' if horizon == StrategyHorizon.LONG_TERM else '短期（1小时）'}</b>："
        f"消息 {w['news']*100:.0f} / 数据 {w['data']*100:.0f} / "
        f"技术 {w['tech']*100:.0f} / 预测 {w['prediction']*100:.0f}。"
    )
    if not snap.is_full_cs:
        note += f" <b>partial_CS</b>（缺 {snap.missing_dimensions}，权重已重归一化）。"
    if snap.overridden:
        note += " <b>黑天鹅覆盖中</b>。"

    liq_thr = getattr(snap, "black_swan_liq_threshold", 200_000_000) or 200_000_000
    spr_thr = getattr(snap, "spread_black_swan_mult", 5.0) or 5.0
    spr = snap.spread_vs_mean
    liq = snap.liq_5m_usd

    return {
        "live": True,
        "tfTag": tf_tag,
        "note": note,
        "label": "合成信号 CS · 量程 <b>−100 ~ +100</b> · 0 为中性",
        "cs": round(cs_display, 1) if cs_display is not None else 0.0,
        "tier": tier,
        "onTier": tier if not snap.overridden else "",
        "tone": tone,
        "contrib": [round(c, 2) for c in contrib],
        "valve": {"ok": valve_ok and not snap.overridden, "txt": valve_txt},
        "faces": faces,
        "mark_price": snap.mark_price,
        "overridden": snap.overridden,
        "suppressed_cs": snap.suppressed_cs,
        "is_full_cs": snap.is_full_cs,
        "staleness": snap.staleness_sec,
        "decision": snap.decision.value,
        "timestamp_ms": snap.timestamp_ms,
        "atr": snap.atr,
        "atr_pct": snap.atr_pct,
        "risk": {
            "short_pct": float(RISK_PER_TRADE_PCT.get("short_term", 0.005)),
            "long_pct": float(RISK_PER_TRADE_PCT.get("long_term", 0.01)),
            "formula": "qty = (equity × risk_pct) / (ATR × hard_sl_atr)",
        },
        "thresholds": DECISION_THRESHOLDS,
        "watch": {
            "liq": _fmt_yi(liq),
            "liq_usd": liq,
            "liq_thr": _fmt_yi(liq_thr),
            "spr": f"{spr:.1f}×" if spr is not None else "—",
            "spr_raw": spr,
            "spr_thr": f"{spr_thr:.1f}×",
            "state": (
                "已触发 · 常规评分暂停"
                if snap.overridden
                else "常规评分运行中"
            ),
        },
    }


class PanelApp:
    def __init__(self, executor=None) -> None:
        # 短期 1h 决策窗口 → CS 10s 重算 + 5m K 线; 长期 30d → 120s 重算即可
        self.loop_short = LiveScoringLoop(
            horizon=StrategyHorizon.SHORT_TERM,
            refresh_sec=10.0,
            kline_interval="1h",
        )
        self.loop_long = LiveScoringLoop(
            horizon=StrategyHorizon.LONG_TERM,
            refresh_sec=120.0,
            kline_interval="4h",
        )
        # 共用采集器: 长期环复用短期的采集实例, 避免双倍请求
        self.loop_long.binance = self.loop_short.binance
        self.loop_long.free = self.loop_short.free
        self.loop_long.polymarket = self.loop_short.polymarket
        self.loop_long.deribit = self.loop_short.deribit
        self.loop_long.onchain = self.loop_short.onchain
        self.loop_long.news = self.loop_short.news
        self.loop_long.cryptopanic = self.loop_short.cryptopanic
        self.loop_long.klines = self.loop_short.klines
        self.loop_long.predict_fun = self.loop_short.predict_fun
        self.loop_long.fear_greed = self.loop_short.fear_greed
        self.loop_long.defi_llama = self.loop_short.defi_llama
        self.loop_long.fred = self.loop_short.fred
        # 评分锁 / stop 均在 start_scoring 里创建 (必须绑在运行中的 event loop 上)
        self._score_lock: Optional[asyncio.Lock] = None
        self.executor = executor  # TradeExecutor | None
        self._stop: Optional[asyncio.Event] = None
        self._tasks: List[asyncio.Task] = []
        self._trade_queue: Optional[asyncio.Queue] = None
        self._trade_worker: Optional[asyncio.Task] = None

    def _schedule_trade(self, snap: LiveScoreSnapshot, horizon: str) -> None:
        if self.executor is None:
            return
        try:
            loop = asyncio.get_running_loop()
            # 串行排队, 禁止并发 fire-and-forget 打穿 _trade_lock 外的状态
            if self._trade_queue is None:
                self._trade_queue = asyncio.Queue()
            self._trade_queue.put_nowait((snap, horizon))
            if self._trade_worker is None or self._trade_worker.done():
                self._trade_worker = loop.create_task(self._trade_worker_loop())
        except RuntimeError:
            pass

    async def _trade_worker_loop(self) -> None:
        if self._trade_queue is None:
            return
        while True:
            try:
                snap, horizon = self._trade_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self.executor.on_snapshot(snap, horizon)
            except Exception as exc:
                logger.warning("trade executor: %s", exc)

    async def start_scoring(self) -> None:
        self._stop = asyncio.Event()
        self._score_lock = asyncio.Lock()
        self.loop_short.bind_score_lock(self._score_lock)
        self.loop_long.bind_score_lock(self._score_lock)

        def _on_short(snap: LiveScoreSnapshot) -> None:
            logger.info(
                "short mark=%s CS=%s → %s atr=%s",
                snap.mark_price,
                snap.composite_score if snap.is_full_cs else snap.partial_cs,
                snap.decision.value,
                snap.atr,
            )
            self._schedule_trade(snap, "short_term")

        self._tasks.append(
            asyncio.create_task(
                self.loop_short.run(stop_event=self._stop, on_score=_on_short)
            )
        )

        async def _long_poll():
            await asyncio.sleep(8)
            while self._stop is not None and not self._stop.is_set():
                t0 = time.time()
                try:
                    snap = await asyncio.wait_for(self.loop_long.score_once(), timeout=60.0)
                    logger.info(
                        "long mark=%s CS=%s → %s atr=%s",
                        snap.mark_price,
                        snap.composite_score if snap.is_full_cs else snap.partial_cs,
                        snap.decision.value,
                        snap.atr,
                    )
                    self._schedule_trade(snap, "long_term")
                except asyncio.TimeoutError:
                    logger.warning("long score_once timeout (>60s)")
                    if self.executor and getattr(self.executor, "guardian", None):
                        self.executor.guardian.note_score_result(False)
                except Exception as exc:
                    logger.warning("long score: %s", exc)
                    if self.executor and getattr(self.executor, "guardian", None):
                        self.executor.guardian.note_score_result(False)
                elapsed = time.time() - t0
                wait = max(5.0, 120.0 - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

        self._tasks.append(asyncio.create_task(_long_poll()))

        # 独立风控看门狗 — 评分失败时仍运行
        if self.executor is not None and getattr(self.executor, "guardian", None):
            self._tasks.append(
                asyncio.create_task(self.executor.run_guardian(self._stop))
            )

    async def stop_scoring(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self.loop_short.stop()
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.executor is not None:
            try:
                await self.executor.close()
            except Exception:
                pass

    def latest_for(self, horizon: str) -> Optional[LiveScoreSnapshot]:
        if horizon in ("long", "long_term"):
            return self.loop_long.latest or self.loop_short.latest
        return self.loop_short.latest


def create_app(panel: Optional[PanelApp] = None) -> "web.Application":
    if web is None:
        raise RuntimeError("aiohttp required")
    panel = panel or PanelApp()
    app = web.Application()
    app["panel"] = panel

    async def on_start(_app):
        await panel.start_scoring()

    async def on_stop(_app):
        await panel.stop_scoring()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)

    async def index(_request):
        html_path = PANEL_HTML
        if not html_path.exists():
            return web.Response(text="panel html missing", status=404)
        return web.FileResponse(html_path)

    async def api_live(request):
        horizon = request.rel_url.query.get("horizon", "short")
        snap = panel.latest_for(horizon)
        if snap is None:
            return web.json_response(
                {"live": False, "error": "warming_up"}, status=503
            )
        hz = (
            StrategyHorizon.LONG_TERM
            if horizon in ("long", "long_term")
            else StrategyHorizon.SHORT_TERM
        )
        # 若要长期口径但 long 尚未算出, 用 short 快照按长期权重临时映射
        if hz == StrategyHorizon.LONG_TERM and panel.loop_long.latest is None:
            # 强制用 short 的 detail 重算会更准, 这里先返回 short 结构并标注
            payload = snapshot_to_panel(snap, StrategyHorizon.SHORT_TERM)
            payload["warning"] = "long_term warming, showing short_term"
            return web.json_response(payload)
        return web.json_response(snapshot_to_panel(snap, hz))

    async def api_health(_request):
        s = panel.loop_short.latest
        return web.json_response({
            "ok": True,
            "has_snapshot": s is not None,
            "full_cs": bool(s and s.is_full_cs),
            "journal_records": 0,  # review 闭环未接; 占位避免面板模板炸
            "key_configured": False,
        })

    app.router.add_get("/", index)
    app.router.add_get("/api/live", api_live)
    app.router.add_get("/api/health", api_health)
    return app


def main() -> None:
    """兼容入口 — 完整 API 在 review.panel_server."""
    from review.panel_server import main as review_main
    review_main()


if __name__ == "__main__":
    main()
