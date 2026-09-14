"""免费衍生品数据源 — 替代收费 CoinGlass.

数据来源 (全部公开、无需 API Key):
  - Binance Futures: OI 历史、全局多空比、强平流 forceOrder
  - Bybit: 线性合约 OI
  - OKX: SWAP OI (USD)

清算热力图: 无稳定免费全网热力图 API。
  本模块用「近期强平价位在 ±1~3% 的分布」做粗粒度磁铁近似;
  若样本不足则 heatmap 记 None → mapper 记 0。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    BINANCE_FUTURES_REST,
    BINANCE_FUTURES_WS,
    BINANCE_SYMBOL,
    BLACK_SWAN_LIQ_5M_USD,
    COINGLASS_POLL_INTERVAL_SEC,
    HEATMAP_BAND_HIGH_PCT,
    HEATMAP_BAND_LOW_PCT,
)
from models.snapshots import CoinGlassSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 纯函数解析
# ---------------------------------------------------------------------------

def parse_binance_oi_hist(rows: list) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Binance openInterestHist → (latest_oi_usd, ch_5m, ch_24h)."""
    if not rows:
        return None, None, None
    parsed = []
    for r in rows:
        ts = int(r.get("timestamp", 0))
        oi = float(r.get("sumOpenInterestValue") or r.get("sumOpenInterest") or 0)
        parsed.append((ts, oi))
    parsed.sort(key=lambda x: x[0])
    latest_ts, latest = parsed[-1]
    if latest <= 0:
        return None, None, None

    def pct_ago(ms: int) -> Optional[float]:
        target = latest_ts - ms
        cand = None
        for t, o in parsed:
            if t <= target:
                cand = o
        if cand is None or cand == 0:
            return None
        return (latest - cand) / cand

    ch5 = pct_ago(5 * 60 * 1000)
    ch24 = pct_ago(24 * 60 * 60 * 1000)
    if ch5 is None and len(parsed) >= 2 and parsed[-2][1]:
        ch5 = (latest - parsed[-2][1]) / parsed[-2][1]
    if ch24 is None and parsed[0][1]:
        ch24 = (latest - parsed[0][1]) / parsed[0][1]
    return latest, ch5, ch24


def parse_binance_lsr(rows: list) -> Optional[float]:
    if not rows:
        return None
    last = rows[-1]
    return float(last.get("longShortRatio") or 0) or None


def parse_bybit_oi_btc(payload: dict) -> Optional[float]:
    """Bybit linear OI 单位是 BTC 张数(双边合计 openInterest)."""
    result = payload.get("result") or {}
    lst = result.get("list") or []
    if not lst:
        return None
    return float(lst[0].get("openInterest") or 0) or None


def parse_okx_oi_usd(payload: dict) -> Optional[float]:
    data = payload.get("data") or []
    if not data:
        return None
    return float(data[0].get("oiUsd") or 0) or None


def heatmap_from_liquidations(
    events: List[Tuple[float, float, bool]],
    mark_price: float,
    band_low: float = HEATMAP_BAND_LOW_PCT,
    band_high: float = HEATMAP_BAND_HIGH_PCT,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """用强平事件粗算磁铁.

    events: (price, notional_usd, is_long_liq)
    上方密集的空头强平 → 做多磁铁 (+);
    下方密集的多头强平 → 做空磁铁 (-).
    """
    if mark_price <= 0 or not events:
        return None, None, None
    above = 0.0  # 上方发生的空头爆仓 (SHORT liq) 名义额
    below = 0.0  # 下方发生的多头爆仓 (LONG liq)
    for price, notional, is_long in events:
        dist = (price - mark_price) / mark_price
        ad = abs(dist)
        if not (band_low <= ad <= band_high):
            continue
        if dist > 0 and not is_long:
            above += notional
        elif dist < 0 and is_long:
            below += notional
        elif dist > 0 and is_long:
            # 上方多头爆仓较少见, 计入上方压力但不作为空头磁铁主信号
            above += notional * 0.3
        elif dist < 0 and not is_long:
            below += notional * 0.3

    total = above + below
    if total <= 0:
        return 0.0, 0.0, 0.0
    above_i = above / total
    below_i = below / total
    return above_i, below_i, above_i - below_i


class ForceOrderBuffer:
    """滚动窗口内的强平事件 + 30min 峰值跟踪 (清算速度)."""

    def __init__(self, window_sec: float = 300.0, peak_window_sec: float = 1800.0) -> None:
        self.window_sec = window_sec
        self.peak_window_sec = peak_window_sec
        # (ts, price, notional, is_long_liq)
        self._events: Deque[Tuple[float, float, float, bool]] = deque()
        # 峰值采样: (ts, long_5m_total, short_5m_total)
        self._peak_samples: Deque[Tuple[float, float, float]] = deque()
        self._peak_long: float = 0.0
        self._peak_short: float = 0.0

    def add(self, price: float, qty: float, side: str, ts_ms: int) -> None:
        """side: 强平单的挂单方向. SELL = 多头被强平, BUY = 空头被强平."""
        notional = abs(price * qty)
        is_long_liq = side.upper() == "SELL"
        now = ts_ms / 1000.0
        self._events.append((now, price, notional, is_long_liq))
        self._prune(now)
        long_usd, short_usd, _ = self.totals()
        self._peak_samples.append((now, long_usd, short_usd))
        self._prune_peaks(now)
        self._peak_long = max(self._peak_long, long_usd)
        self._peak_short = max(self._peak_short, short_usd)
        # 从样本重算峰值 (窗口滑动后)
        if self._peak_samples:
            self._peak_long = max(s[1] for s in self._peak_samples)
            self._peak_short = max(s[2] for s in self._peak_samples)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _prune_peaks(self, now: float) -> None:
        cutoff = now - self.peak_window_sec
        while self._peak_samples and self._peak_samples[0][0] < cutoff:
            self._peak_samples.popleft()

    def totals(self) -> Tuple[float, float, float]:
        long_usd = sum(n for _, _, n, is_long in self._events if is_long)
        short_usd = sum(n for _, _, n, is_long in self._events if not is_long)
        return long_usd, short_usd, long_usd + short_usd

    def cleared_ratios(self) -> Tuple[Optional[float], Optional[float]]:
        """相对 30min 峰值的清除比例 [0,1]; 峰值过低则 None."""
        long_usd, short_usd, _ = self.totals()
        long_cleared = None
        short_cleared = None
        if self._peak_long >= 1_000_000:  # 至少 100 万峰值才有意义
            long_cleared = max(0.0, min(1.0, 1.0 - long_usd / self._peak_long))
        if self._peak_short >= 1_000_000:
            short_cleared = max(0.0, min(1.0, 1.0 - short_usd / self._peak_short))
        return long_cleared, short_cleared

    def as_heatmap_events(self) -> List[Tuple[float, float, bool]]:
        return [(p, n, is_long) for _, p, n, is_long in self._events]


def parse_force_order_event(data: dict) -> Optional[dict]:
    """解析 Binance forceOrder 推送."""
    o = data.get("o") or data
    try:
        price = float(o.get("p") or o.get("ap") or 0)
        qty = float(o.get("q") or o.get("l") or 0)
        side = str(o.get("S") or "")
        ts = int(o.get("T") or data.get("E") or time.time() * 1000)
    except (TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0 or side not in ("BUY", "SELL"):
        return None
    return {"price": price, "qty": qty, "side": side, "ts_ms": ts}


class FreeDerivativesCollector:
    """免费衍生品采集器, 输出与 CoinGlassSnapshot 兼容的快照."""

    def __init__(
        self,
        symbol: str = BINANCE_SYMBOL,
        rest_base: str = BINANCE_FUTURES_REST,
        ws_base: str = BINANCE_FUTURES_WS,
        poll_interval: float = COINGLASS_POLL_INTERVAL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.symbol_lower = self.symbol.lower()
        self.rest_base = rest_base.rstrip("/")
        self.ws_base = ws_base
        self.poll_interval = poll_interval
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None  # lazy asyncio.Lock
        self._snapshot = CoinGlassSnapshot()
        self._force = ForceOrderBuffer(300.0)
        self._running = False
        self._mark_price: Optional[float] = None
        self.source = "binance+bybit+okx"
        self.last_error: Optional[str] = None

    @property
    def snapshot(self) -> CoinGlassSnapshot:
        return self._snapshot

    def get_snapshot(self) -> CoinGlassSnapshot:
        s = self._snapshot
        return CoinGlassSnapshot(
            open_interest_usd=s.open_interest_usd,
            oi_change_5m_pct=s.oi_change_5m_pct,
            oi_change_24h_pct=s.oi_change_24h_pct,
            heatmap_above_intensity=s.heatmap_above_intensity,
            heatmap_below_intensity=s.heatmap_below_intensity,
            heatmap_magnet=s.heatmap_magnet,
            liq_long_5m_usd=s.liq_long_5m_usd,
            liq_short_5m_usd=s.liq_short_5m_usd,
            liq_total_5m_usd=s.liq_total_5m_usd,
            long_short_ratio=s.long_short_ratio,
            liquidation_speed_long_cleared=s.liquidation_speed_long_cleared,
            liquidation_speed_short_cleared=s.liquidation_speed_short_cleared,
            available=s.available,
            last_success_ts=s.last_success_ts,
            last_error=s.last_error,
        )

    def set_mark_price(self, price: float) -> None:
        self._mark_price = price

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        # 防并发创建双 session → 被覆盖者 GC 关掉 → Connector is closed
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def _get_json(self, url: str, params: Optional[dict] = None) -> Optional[Any]:
        session = await self._ensure_session()
        try:
            async with session.get(url, params=params or {}, timeout=15) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    self.last_error = f"{url} HTTP {resp.status}: {text[:120]}"
                    logger.warning("free_deriv %s", self.last_error)
                    return None
                return await resp.json()
        except Exception as exc:
            self.last_error = f"{url}: {exc}"
            logger.warning("free_deriv error %s", self.last_error)
            return None

    async def fetch_once(self, mark_price: Optional[float] = None) -> CoinGlassSnapshot:
        price = mark_price or self._mark_price
        bn_hist, bn_lsr, bybit, okx = await asyncio.gather(
            self._get_json(
                f"{self.rest_base}/futures/data/openInterestHist",
                {"symbol": self.symbol, "period": "5m", "limit": 300},
            ),
            self._get_json(
                f"{self.rest_base}/futures/data/globalLongShortAccountRatio",
                {"symbol": self.symbol, "period": "5m", "limit": 12},
            ),
            self._get_json(
                "https://api.bybit.com/v5/market/open-interest",
                {"category": "linear", "symbol": self.symbol, "intervalTime": "5min", "limit": 3},
            ),
            self._get_json(
                "https://www.okx.com/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
            ),
        )

        snap = CoinGlassSnapshot()
        got = False

        if isinstance(bn_hist, list):
            oi, ch5, ch24 = parse_binance_oi_hist(bn_hist)
            # 聚合: Binance USD + Bybit BTC*price + OKX USD
            agg = oi or 0.0
            if bybit and price:
                btc_oi = parse_bybit_oi_btc(bybit)
                if btc_oi:
                    agg += btc_oi * price
            if okx:
                okx_usd = parse_okx_oi_usd(okx)
                if okx_usd:
                    agg += okx_usd
            if agg > 0:
                snap.open_interest_usd = agg
                snap.oi_change_5m_pct = ch5
                snap.oi_change_24h_pct = ch24
                got = True

        if isinstance(bn_lsr, list):
            snap.long_short_ratio = parse_binance_lsr(bn_lsr)
            if snap.long_short_ratio is not None:
                got = True

        long_u, short_u, total = self._force.totals()
        snap.liq_long_5m_usd = long_u
        snap.liq_short_5m_usd = short_u
        snap.liq_total_5m_usd = total
        long_c, short_c = self._force.cleared_ratios()
        # 无峰值时记 0 (平静市), 避免 liquidation_speed 长期缺项
        snap.liquidation_speed_long_cleared = 0.0 if long_c is None else long_c
        snap.liquidation_speed_short_cleared = 0.0 if short_c is None else short_c
        if total > 0:
            got = True

        # WS 强平样本不足时, 用 OKX 公开清算单做热力图兜底
        if price and not self._force.as_heatmap_events():
            try:
                await self._seed_okx_liquidations(price)
            except Exception as exc:
                logger.warning("okx liquidation seed failed: %s", exc)
            long_u, short_u, total = self._force.totals()
            snap.liq_long_5m_usd = long_u
            snap.liq_short_5m_usd = short_u
            snap.liq_total_5m_usd = total
            long_c, short_c = self._force.cleared_ratios()
            snap.liquidation_speed_long_cleared = 0.0 if long_c is None else long_c
            snap.liquidation_speed_short_cleared = 0.0 if short_c is None else short_c
            if total > 0:
                got = True

        if price and self._force.as_heatmap_events():
            above, below, magnet = heatmap_from_liquidations(
                self._force.as_heatmap_events(), price
            )
            snap.heatmap_above_intensity = above
            snap.heatmap_below_intensity = below
            snap.heatmap_magnet = magnet
            if magnet is not None:
                got = True
        elif price:
            # 无爆仓样本时记中性磁铁 0, 避免长期 missing 占权重
            snap.heatmap_above_intensity = 0.0
            snap.heatmap_below_intensity = 0.0
            snap.heatmap_magnet = 0.0
            got = True

        snap.available = got
        if got:
            snap.last_success_ts = int(time.time() * 1000)
            self.last_error = None
        self._snapshot = snap
        return self.get_snapshot()

    async def _seed_okx_liquidations(self, mark_price: float) -> None:
        """OKX 公开清算订单 → ForceOrderBuffer (无鉴权)."""
        url = "https://www.okx.com/api/v5/public/liquidation-orders"
        payload = await self._get_json(
            url,
            {
                "instType": "SWAP",
                "uly": "BTC-USDT",
                "state": "filled",
            },
        )
        if not isinstance(payload, dict):
            return
        rows = payload.get("data") or []
        # OKX 结构: data[].details[] 或扁平
        details: List[dict] = []
        for block in rows:
            if isinstance(block, dict) and block.get("details"):
                details.extend(block["details"])
            elif isinstance(block, dict):
                details.append(block)
        now_ms = int(time.time() * 1000)
        for d in details[-80:]:
            try:
                px = float(d.get("bkPx") or d.get("px") or d.get("bailPrice") or 0)
                sz = float(d.get("sz") or d.get("accFillSz") or 0)
                side = str(d.get("side") or "").upper()
                ts = int(d.get("ts") or now_ms)
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0:
                continue
            # OKX side: sell = 多头被强平; buy = 空头被强平 (与 Binance forceOrder 一致)
            if side not in ("BUY", "SELL"):
                # posSide long/short 兜底
                pos = str(d.get("posSide") or "").lower()
                if pos == "long":
                    side = "SELL"
                elif pos == "short":
                    side = "BUY"
                else:
                    continue
            # OKX sz 多为张; BTC-USDT SWAP 约 0.01 BTC/张 → 用名义粗算
            qty_btc = sz * 0.01
            self._force.add(px, qty_btc, side, ts)

    def handle_force_order_message(self, payload: dict) -> None:
        if "stream" in payload and "data" in payload:
            data = payload["data"]
        else:
            data = payload
        parsed = parse_force_order_event(data)
        if not parsed:
            return
        self._force.add(parsed["price"], parsed["qty"], parsed["side"], parsed["ts_ms"])

    async def run(
        self,
        stop_event: Optional[asyncio.Event] = None,
        mark_price_provider: Optional[Any] = None,
    ) -> None:
        self._running = True
        own_stop = stop_event is None
        stop = stop_event or asyncio.Event()
        poll_task = asyncio.create_task(self._poll_loop(stop, mark_price_provider))
        ws_task = asyncio.create_task(self._force_ws_loop(stop))
        try:
            await stop.wait()
        finally:
            self._running = False
            if own_stop:
                stop.set()
            for t in (poll_task, ws_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self.close()

    async def _poll_loop(self, stop: asyncio.Event, mark_price_provider) -> None:
        while self._running and not stop.is_set():
            price = None
            if mark_price_provider is not None:
                try:
                    price = mark_price_provider()
                except Exception:
                    price = self._mark_price
            await self.fetch_once(mark_price=price)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _force_ws_loop(self, stop: asyncio.Event) -> None:
        """订阅 btcusdt@forceOrder 积累 5min 爆仓."""
        if aiohttp is None:
            return
        url = f"{self.ws_base}?streams={self.symbol_lower}@forceOrder"
        backoff = 1.0
        while self._running and not stop.is_set():
            try:
                session = await self._ensure_session()
                async with session.ws_connect(url, heartbeat=20) as ws:
                    backoff = 1.0
                    logger.info("free_deriv forceOrder WS connected")
                    while self._running and not stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.handle_force_order_message(json.loads(msg.data))
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("forceOrder WS error: %s — retry %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


def is_black_swan_liquidation(snap: CoinGlassSnapshot) -> bool:
    total = snap.liq_total_5m_usd
    return total is not None and total >= BLACK_SWAN_LIQ_5M_USD
