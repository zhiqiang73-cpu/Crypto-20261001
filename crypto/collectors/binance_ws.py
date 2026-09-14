"""Binance USDⓈ-M Futures WebSocket 采集器.

订阅:
  - btcusdt@aggTrade        : 过滤 > $500k 大单
  - btcusdt@markPrice@1s    : 资金费率 + 标记价格
  - btcusdt@depth@100ms     : 增量深度 (配合 REST snapshot 维护本地订单簿)

±2% Bid/Ask 比率基于 depth limit=1000 本地簿计算, 不用 depth20.
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

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore

from config.mapping import (
    BINANCE_DEPTH_LIMIT,
    BINANCE_FUTURES_REST,
    BINANCE_FUTURES_WS,
    BINANCE_RECONNECT_BASE_SEC,
    BINANCE_RECONNECT_MAX_SEC,
    BINANCE_SYMBOL,
    BLOCK_TRADE_NOTIONAL_USD,
    BLOCK_TRADE_WINDOW_SEC,
    FUNDING_PERIOD_HOURS_DEFAULT,
    ORDERBOOK_BAND_PCT,
    SPREAD_MEAN_WINDOW,
)
from models.snapshots import BinanceMicroSnapshot, BlockTradeBias
from utils.scoring import annualize_funding_rate, infer_funding_period_hours

logger = logging.getLogger(__name__)


class LocalOrderBook:
    """维护本地订单簿: REST snapshot + WS depth diff (pu 连续性校验)."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_update_id: Optional[int] = None
        self.synced: bool = False
        self._buffer: List[dict] = []
        self._buffering: bool = True

    def apply_snapshot(self, snapshot: dict) -> None:
        self.bids.clear()
        self.asks.clear()
        for price_s, qty_s in snapshot.get("bids", []):
            price, qty = float(price_s), float(qty_s)
            if qty > 0:
                self.bids[price] = qty
        for price_s, qty_s in snapshot.get("asks", []):
            price, qty = float(price_s), float(qty_s)
            if qty > 0:
                self.asks[price] = qty
        self.last_update_id = int(snapshot["lastUpdateId"])
        self.synced = False
        self._buffering = True

    def buffer_event(self, event: dict) -> None:
        self._buffer.append(event)

    def try_sync_from_buffer(self) -> bool:
        """按 Binance 文档丢弃过期事件, 找到第一个有效 diff 后切换到实时模式."""
        if self.last_update_id is None:
            return False

        # 丢弃 u < lastUpdateId 的事件
        while self._buffer and int(self._buffer[0]["u"]) < self.last_update_id:
            self._buffer.pop(0)

        if not self._buffer:
            return False

        first = self._buffer[0]
        # 第一个事件须满足 U <= lastUpdateId 且 u >= lastUpdateId
        U = int(first["U"])
        u = int(first["u"])
        if not (U <= self.last_update_id and u >= self.last_update_id):
            # 缓冲与 snapshot 对不上, 需要重新拉 snapshot
            self._buffer.clear()
            self.synced = False
            return False

        for event in self._buffer:
            if not self._apply_event(event, require_pu=False):
                self._buffer.clear()
                self.synced = False
                return False

        self._buffer.clear()
        self._buffering = False
        self.synced = True
        return True

    def on_depth_event(self, event: dict) -> bool:
        if self._buffering or not self.synced:
            self.buffer_event(event)
            return self.try_sync_from_buffer()
        ok = self._apply_event(event, require_pu=True)
        if not ok:
            self.synced = False
            self._buffering = True
            self._buffer.clear()
        return ok

    def _apply_event(self, event: dict, require_pu: bool) -> bool:
        if self.last_update_id is None:
            return False
        U = int(event["U"])
        u = int(event["u"])
        pu = int(event.get("pu", -1))

        if require_pu and pu != self.last_update_id:
            logger.warning(
                "orderbook gap: pu=%s last=%s — resync needed",
                pu, self.last_update_id,
            )
            return False

        if u < self.last_update_id:
            return True  # 过期, 忽略

        self._merge_side(self.bids, event.get("b", []))
        self._merge_side(self.asks, event.get("a", []))
        self.last_update_id = u
        return True

    @staticmethod
    def _merge_side(book: Dict[float, float], levels: List[List[str]]) -> None:
        for price_s, qty_s in levels:
            price, qty = float(price_s), float(qty_s)
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty

    def best_bid_ask(self) -> Tuple[Optional[float], Optional[float]]:
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None
        return best_bid, best_ask

    def depth_within_band(self, band_pct: float = ORDERBOOK_BAND_PCT) -> Tuple[float, float, Optional[float]]:
        """返回 (bid_qty, ask_qty, mid). mid 为 None 时两侧为 0."""
        best_bid, best_ask = self.best_bid_ask()
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
            return 0.0, 0.0, None
        if best_bid >= best_ask:
            return 0.0, 0.0, None  # crossed book
        mid = (best_bid + best_ask) / 2.0
        bid_floor = mid * (1.0 - band_pct)
        ask_ceil = mid * (1.0 + band_pct)
        bid_qty = sum(q for p, q in self.bids.items() if p >= bid_floor)
        ask_qty = sum(q for p, q in self.asks.items() if p <= ask_ceil)
        return bid_qty, ask_qty, mid

    def bid_ask_ratio(self, band_pct: float = ORDERBOOK_BAND_PCT) -> Optional[float]:
        bid_qty, ask_qty, mid = self.depth_within_band(band_pct)
        if mid is None or ask_qty <= 0:
            return None
        return bid_qty / ask_qty


class BlockTradeTracker:
    """滚动窗口内大单买卖状态机."""

    def __init__(
        self,
        notional_threshold: float = BLOCK_TRADE_NOTIONAL_USD,
        window_sec: float = BLOCK_TRADE_WINDOW_SEC,
    ) -> None:
        self.notional_threshold = notional_threshold
        self.window_sec = window_sec
        # (ts_sec, side_buy: bool, notional, price)
        self._trades: Deque[Tuple[float, bool, float, float]] = deque()
        self._price_at_window_start: Optional[float] = None

    def on_agg_trade(self, price: float, qty: float, is_buyer_maker: bool, ts_ms: int) -> None:
        notional = price * qty
        if notional < self.notional_threshold:
            self._prune(ts_ms / 1000.0)
            return
        ts = ts_ms / 1000.0
        is_buy = not is_buyer_maker  # m=False → 主动买
        self._trades.append((ts, is_buy, notional, price))
        self._prune(ts)

    def _prune(self, now_sec: float) -> None:
        cutoff = now_sec - self.window_sec
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()
        if self._trades:
            self._price_at_window_start = self._trades[0][3]
        else:
            self._price_at_window_start = None

    def summary(self, current_price: Optional[float] = None) -> Tuple[BlockTradeBias, float, float, int]:
        buy_n = sum(n for _, is_buy, n, _ in self._trades if is_buy)
        sell_n = sum(n for _, is_buy, n, _ in self._trades if not is_buy)
        count = len(self._trades)
        if count == 0 or current_price is None or self._price_at_window_start is None:
            return BlockTradeBias.NEUTRAL, buy_n, sell_n, count

        price_up = current_price > self._price_at_window_start * 1.0001
        price_down = current_price < self._price_at_window_start * 0.9999
        price_flat = not price_up and not price_down

        if buy_n > sell_n * 1.5:
            if price_flat or not price_down:
                if price_up:
                    return BlockTradeBias.BUY_FOLLOW, buy_n, sell_n, count
                return BlockTradeBias.ACCUMULATION, buy_n, sell_n, count
        if sell_n > buy_n * 1.5:
            if price_down:
                return BlockTradeBias.DISTRIBUTION, buy_n, sell_n, count
            if price_flat or not price_up:
                return BlockTradeBias.SELL_ABSORBED, buy_n, sell_n, count

        return BlockTradeBias.NEUTRAL, buy_n, sell_n, count


class CVDTracker:
    """全量 aggTrade 的 5min CVD 滚动累加 (taker 买 − taker 卖, USD)."""

    def __init__(self, window_sec: float = 300.0) -> None:
        self.window_sec = window_sec
        # (ts_sec, signed_notional)
        self._events: Deque[Tuple[float, float]] = deque()

    def on_trade(self, price: float, qty: float, is_buyer_maker: bool, ts_ms: int) -> None:
        notional = price * qty
        signed = -notional if is_buyer_maker else notional
        ts = ts_ms / 1000.0
        self._events.append((ts, signed))
        self._prune(ts)

    def _prune(self, now_sec: float) -> None:
        cutoff = now_sec - self.window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def cvd_usd(self) -> float:
        return sum(n for _, n in self._events)


# ---------------------------------------------------------------------------
# 纯函数解析器 (便于单测, 不依赖网络)
# ---------------------------------------------------------------------------

def parse_mark_price_event(data: dict) -> dict:
    """解析 markPrice 推送, 返回中间字段 dict."""
    period_rate = float(data.get("r", 0))
    event_time = int(data.get("E", 0)) or None
    next_funding = int(data.get("T", 0)) or None
    period_hours = infer_funding_period_hours(
        next_funding, event_time, FUNDING_PERIOD_HOURS_DEFAULT
    )
    # 倒计时推断不可靠时固定用 8h 年化 (手册标准)
    annual = annualize_funding_rate(period_rate, FUNDING_PERIOD_HOURS_DEFAULT)
    return {
        "mark_price": float(data.get("p", 0)),
        "index_price": float(data.get("i", 0)) if data.get("i") else None,
        "funding_rate_period": period_rate,
        "funding_rate_annualized": annual,
        "funding_period_hours": FUNDING_PERIOD_HOURS_DEFAULT,
        "next_funding_time_ms": next_funding,
        "event_time_ms": event_time,
    }


def parse_agg_trade_event(data: dict) -> Optional[dict]:
    """解析 aggTrade; 若低于大单门槛返回 None."""
    price = float(data["p"])
    qty = float(data["q"])
    notional = price * qty
    if notional < BLOCK_TRADE_NOTIONAL_USD:
        return None
    return {
        "price": price,
        "qty": qty,
        "notional": notional,
        "is_buyer_maker": bool(data["m"]),
        "is_buy": not bool(data["m"]),
        "trade_time_ms": int(data.get("T", data.get("E", 0))),
    }


def compute_spread_metrics(
    best_bid: Optional[float],
    best_ask: Optional[float],
    spread_history: Deque[float],
) -> Tuple[Optional[float], Optional[float]]:
    """返回 (spread, spread_vs_mean)."""
    if best_bid is None or best_ask is None or best_ask <= best_bid:
        return None, None
    spread = best_ask - best_bid
    spread_history.append(spread)
    mean = sum(spread_history) / len(spread_history) if spread_history else spread
    vs_mean = spread / mean if mean > 0 else 1.0
    return spread, vs_mean


class BinanceFuturesCollector:
    """异步 Binance Futures 采集器 — 持续更新 BinanceMicroSnapshot."""

    def __init__(
        self,
        symbol: str = BINANCE_SYMBOL,
        rest_base: str = BINANCE_FUTURES_REST,
        ws_base: str = BINANCE_FUTURES_WS,
        session: Optional[Any] = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.symbol_lower = self.symbol.lower()
        self.rest_base = rest_base.rstrip("/")
        self.ws_base = ws_base
        self._external_session = session
        self._session: Optional[Any] = None

        self.book = LocalOrderBook()
        self.blocks = BlockTradeTracker()
        self.cvd = CVDTracker(window_sec=300.0)
        self._spread_history: Deque[float] = deque(maxlen=SPREAD_MEAN_WINDOW)
        self._snapshot = BinanceMicroSnapshot()
        self._running = False
        self._resync_lock: Optional[asyncio.Lock] = None

    @property
    def snapshot(self) -> BinanceMicroSnapshot:
        return self._snapshot

    def get_snapshot(self) -> BinanceMicroSnapshot:
        """返回当前快照的浅拷贝字段集合."""
        s = self._snapshot
        return BinanceMicroSnapshot(
            mark_price=s.mark_price,
            index_price=s.index_price,
            funding_rate_period=s.funding_rate_period,
            funding_rate_annualized=s.funding_rate_annualized,
            funding_period_hours=s.funding_period_hours,
            next_funding_time_ms=s.next_funding_time_ms,
            bid_ask_ratio_2pct=s.bid_ask_ratio_2pct,
            bid_depth_2pct=s.bid_depth_2pct,
            ask_depth_2pct=s.ask_depth_2pct,
            best_bid=s.best_bid,
            best_ask=s.best_ask,
            spread=s.spread,
            spread_vs_mean=s.spread_vs_mean,
            block_trade_bias=s.block_trade_bias,
            block_buy_notional_usd=s.block_buy_notional_usd,
            block_sell_notional_usd=s.block_sell_notional_usd,
            block_trade_count=s.block_trade_count,
            cvd_5m_usd=s.cvd_5m_usd,
            orderbook_synced=s.orderbook_synced,
            event_time_ms=s.event_time_ms,
        )

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for BinanceFuturesCollector")
        if self._session is None or self._session.closed:
            import socket
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
            )
        return self._session

    async def fetch_depth_snapshot(self) -> dict:
        session = await self._ensure_session()
        url = f"{self.rest_base}/fapi/v1/depth"
        params = {"symbol": self.symbol, "limit": BINANCE_DEPTH_LIMIT}
        async with session.get(url, params=params, timeout=10) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def fetch_premium_index(self) -> dict:
        """REST 兜底: mark price + funding rate."""
        session = await self._ensure_session()
        url = f"{self.rest_base}/fapi/v1/premiumIndex"
        params = {"symbol": self.symbol}
        async with session.get(url, params=params, timeout=10) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def seed_mark_price_from_rest(self) -> None:
        """WS 未就绪时用 REST 填上 mark/funding, 避免快照全空."""
        try:
            data = await self.fetch_premium_index()
        except Exception as exc:
            logger.warning("premiumIndex REST failed: %s", exc)
            return
        period = float(data.get("lastFundingRate", 0) or 0)
        mark = float(data.get("markPrice", 0) or 0)
        event_ms = int(data.get("time", time.time() * 1000))
        next_ms = int(data.get("nextFundingTime", 0) or 0) or None
        synthetic = {
            "e": "markPriceUpdate",
            "E": event_ms,
            "s": self.symbol,
            "p": str(mark),
            "i": data.get("indexPrice"),
            "r": str(period),
            "T": next_ms or 0,
        }
        self._on_mark_price(synthetic)

    async def resync_orderbook(self) -> None:
        """REST snapshot + 已缓冲的 depth 事件对齐 (Binance 官方顺序)."""
        if self._resync_lock is None:
            self._resync_lock = asyncio.Lock()
        async with self._resync_lock:
            logger.info("resyncing orderbook snapshot for %s", self.symbol)
            buffered = list(self.book._buffer)
            snap = await self.fetch_depth_snapshot()
            self.book.apply_snapshot(snap)
            self.book._buffer = buffered
            self.book._buffering = True
            self.book.try_sync_from_buffer()
            self._refresh_book_metrics()

    def _refresh_book_metrics(self) -> None:
        best_bid, best_ask = self.book.best_bid_ask()
        bid_qty, ask_qty, _mid = self.book.depth_within_band()
        ratio = self.book.bid_ask_ratio()
        spread, vs_mean = compute_spread_metrics(best_bid, best_ask, self._spread_history)

        self._snapshot.best_bid = best_bid
        self._snapshot.best_ask = best_ask
        self._snapshot.bid_depth_2pct = bid_qty
        self._snapshot.ask_depth_2pct = ask_qty
        self._snapshot.bid_ask_ratio_2pct = ratio
        self._snapshot.spread = spread
        self._snapshot.spread_vs_mean = vs_mean
        self._snapshot.orderbook_synced = self.book.synced

    def handle_message(self, payload: dict) -> None:
        """处理组合流或单流消息 (纯逻辑, 可单测)."""
        # 组合流格式: {"stream": "...", "data": {...}}
        if "stream" in payload and "data" in payload:
            stream = payload["stream"]
            data = payload["data"]
        else:
            data = payload
            etype = data.get("e", "")
            if etype == "aggTrade":
                stream = f"{self.symbol_lower}@aggTrade"
            elif etype == "markPriceUpdate":
                stream = f"{self.symbol_lower}@markPrice"
            elif etype == "depthUpdate":
                stream = f"{self.symbol_lower}@depth"
            else:
                return

        if "aggTrade" in stream:
            self._on_agg_trade(data)
        elif "markPrice" in stream:
            self._on_mark_price(data)
        elif "depth" in stream:
            self._on_depth(data)

    def _on_agg_trade(self, data: dict) -> None:
        parsed = parse_agg_trade_event(data)
        price = float(data["p"])
        qty = float(data["q"])
        is_buyer_maker = bool(data["m"])
        ts = int(data.get("T", data.get("E", time.time() * 1000)))
        self.blocks.on_agg_trade(price, qty, is_buyer_maker, ts)
        self.cvd.on_trade(price, qty, is_buyer_maker, ts)
        self._snapshot.cvd_5m_usd = self.cvd.cvd_usd()

        bias, buy_n, sell_n, count = self.blocks.summary(
            current_price=self._snapshot.mark_price or price
        )
        self._snapshot.block_trade_bias = bias
        self._snapshot.block_buy_notional_usd = buy_n
        self._snapshot.block_sell_notional_usd = sell_n
        self._snapshot.block_trade_count = count
        self._snapshot.event_time_ms = ts
        if parsed:
            logger.debug("block trade: %s notional=%.0f", bias.value, parsed["notional"])

    def _on_mark_price(self, data: dict) -> None:
        parsed = parse_mark_price_event(data)
        self._snapshot.mark_price = parsed["mark_price"]
        self._snapshot.index_price = parsed["index_price"]
        self._snapshot.funding_rate_period = parsed["funding_rate_period"]
        self._snapshot.funding_rate_annualized = parsed["funding_rate_annualized"]
        self._snapshot.funding_period_hours = parsed["funding_period_hours"]
        self._snapshot.next_funding_time_ms = parsed["next_funding_time_ms"]
        self._snapshot.event_time_ms = parsed["event_time_ms"]
        # 更新大单状态 (用最新标记价)
        bias, buy_n, sell_n, count = self.blocks.summary(
            current_price=self._snapshot.mark_price
        )
        self._snapshot.block_trade_bias = bias
        self._snapshot.block_buy_notional_usd = buy_n
        self._snapshot.block_sell_notional_usd = sell_n
        self._snapshot.block_trade_count = count

    def _on_depth(self, data: dict) -> None:
        if self.book._buffering and self.book.last_update_id is None:
            # snapshot 尚未加载, 先缓冲
            self.book.buffer_event(data)
            return
        ok = self.book.on_depth_event(data)
        if not ok and not self.book.synced:
            # 触发异步重同步 (由 run loop 负责)
            self._snapshot.orderbook_synced = False
        self._refresh_book_metrics()

    def _streams_url(self) -> str:
        streams = "/".join([
            f"{self.symbol_lower}@aggTrade",
            f"{self.symbol_lower}@markPrice@1s",
            f"{self.symbol_lower}@depth@100ms",
        ])
        return f"{self.ws_base}?streams={streams}"

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """主循环: 先连 WS 缓冲 depth → 再拉 snapshot 对齐 → 指数退避重连.

        优先用 aiohttp WebSocket (兼容 HTTP_PROXY); websockets 作为回退.
        """
        self._running = True
        backoff = BINANCE_RECONNECT_BASE_SEC
        stop = stop_event or asyncio.Event()

        while self._running and not stop.is_set():
            try:
                await self.seed_mark_price_from_rest()
                url = self._streams_url()
                logger.info("connecting Binance WS: %s", url)
                # 进入缓冲态, 等 WS 事件进来后再拉 snapshot
                self.book = LocalOrderBook()
                self.book._buffering = True
                try:
                    await asyncio.wait_for(self._run_ws_session(url, stop), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.warning("Binance WS session idle/connect >120s — reconnect")
                backoff = BINANCE_RECONNECT_BASE_SEC
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Binance WS error: %s — reconnect in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BINANCE_RECONNECT_MAX_SEC)

        await self.close()

    async def _run_ws_session(self, url: str, stop: asyncio.Event) -> None:
        session = await self._ensure_session()
        snapshot_task: Optional[asyncio.Task] = None

        async def _delayed_snapshot() -> None:
            await asyncio.sleep(0.8)
            if self._running and not stop.is_set():
                await self.resync_orderbook()

        try:
            async with session.ws_connect(
                url, heartbeat=20, receive_timeout=None, autoclose=True, autoping=True
            ) as ws:
                logger.info("Binance WS connected")
                snapshot_task = asyncio.create_task(_delayed_snapshot())
                while self._running and not stop.is_set():
                    if (
                        not self.book.synced
                        and self.book.last_update_id is not None
                        and len(self.book._buffer) > 800
                    ):
                        # 勿在 receive 循环里同步 await resync — 会堵死 WS 读
                        if self._resync_lock is None or not self._resync_lock.locked():
                            asyncio.create_task(self.resync_orderbook())

                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self.handle_message(json.loads(msg.data))
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        self.handle_message(json.loads(msg.data.decode("utf-8")))
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise ConnectionError(f"WS closed: type={msg.type} extra={ws.exception()}")
        finally:
            if snapshot_task is not None:
                snapshot_task.cancel()
                try:
                    await snapshot_task
                except (asyncio.CancelledError, Exception):
                    pass

    def stop(self) -> None:
        self._running = False

    async def ensure_microstructure(self) -> BinanceMicroSnapshot:
        """评分前兜底: REST 补齐订单簿 / CVD / spread, 避免 WS 暖机期长期缺项."""
        # WS 已就绪则跳过重同步, 防止评分环被 depth REST 拖死
        if (
            self.book.synced
            and self._snapshot.mark_price is not None
            and self._snapshot.bid_ask_ratio_2pct is not None
        ):
            return self.get_snapshot()

        need_book = (
            not self.book.synced
            or self._snapshot.bid_ask_ratio_2pct is None
            or self._snapshot.spread_vs_mean is None
        )
        if need_book:
            try:
                await asyncio.wait_for(self.resync_orderbook(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("ensure_microstructure depth timeout")
            except Exception as exc:
                logger.warning("ensure_microstructure depth failed: %s", exc)

        if self._snapshot.cvd_5m_usd is None:
            try:
                await asyncio.wait_for(self.seed_cvd_from_rest(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("ensure_microstructure cvd timeout")
            except Exception as exc:
                logger.warning("ensure_microstructure cvd failed: %s", exc)

        if self._snapshot.mark_price is None:
            try:
                await asyncio.wait_for(self.seed_mark_price_from_rest(), timeout=5.0)
            except Exception as exc:
                logger.warning("ensure_microstructure mark failed: %s", exc)

        return self.get_snapshot()

    async def seed_cvd_from_rest(self, limit: int = 1000) -> None:
        """用近期 aggTrades REST 种子化 5min CVD (WS 尚未灌满时)."""
        session = await self._ensure_session()
        url = f"{self.rest_base}/fapi/v1/aggTrades"
        params = {"symbol": self.symbol, "limit": limit}
        async with session.get(url, params=params, timeout=10) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        if not isinstance(rows, list):
            return
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(self.cvd.window_sec * 1000)
        # 重建 tracker, 只保留窗口内
        self.cvd = CVDTracker(window_sec=self.cvd.window_sec)
        for r in rows:
            ts = int(r.get("T") or r.get("E") or 0)
            if ts < cutoff:
                continue
            price = float(r.get("p") or 0)
            qty = float(r.get("q") or 0)
            is_buyer_maker = bool(r.get("m"))
            if price > 0 and qty > 0:
                self.cvd.on_trade(price, qty, is_buyer_maker, ts)
        self._snapshot.cvd_5m_usd = self.cvd.cvd_usd()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
