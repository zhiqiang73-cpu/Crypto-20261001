"""Binance Futures K线 REST 采集器 — 技术面 OHLCV 输入."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import BINANCE_FUTURES_REST, BINANCE_SYMBOL
from indicators.ohlcv import Candle

logger = logging.getLogger(__name__)


def parse_klines_payload(rows: list) -> List[Candle]:
    """解析 Binance klines 二维数组."""
    candles: List[Candle] = []
    for row in rows:
        candles.append(Candle(
            open_time_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time_ms=int(row[6]) if len(row) > 6 else 0,
        ))
    return candles


class BinanceKlinesCollector:
    """拉取 BTCUSDT 永续 K 线."""

    def __init__(
        self,
        symbol: str = BINANCE_SYMBOL,
        rest_base: str = BINANCE_FUTURES_REST,
        session: Optional[Any] = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.rest_base = rest_base.rstrip("/")
        self._external_session = session
        self._session: Optional[Any] = None

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp is required")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def fetch_klines(
        self,
        interval: str = "15m",
        limit: int = 300,
    ) -> List[Candle]:
        session = await self._ensure_session()
        url = f"{self.rest_base}/fapi/v1/klines"
        params = {"symbol": self.symbol, "interval": interval, "limit": limit}
        async with session.get(url, params=params, timeout=15) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        return parse_klines_payload(rows)

    async def fetch_tech_inputs(
        self,
        intraday_interval: str = "15m",
        intraday_limit: int = 300,
    ) -> tuple:
        """返回 (intraday_candles, daily_candles)."""
        intra = await self.fetch_klines(intraday_interval, intraday_limit)
        daily = await self.fetch_klines("1d", 30)
        return intra, daily

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
