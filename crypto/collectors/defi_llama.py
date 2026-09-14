"""DefiLlama Stablecoins — USDT 总市值变化 (免费, 无需 key).

用日频市值序列的最近两天差值近似 24h 净铸造量.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import DEFI_LLAMA_POLL_SEC

# 列表接口比单币详情快得多
DEFI_LLAMA_LIST_URL = "https://stablecoins.llama.fi/stablecoins"

logger = logging.getLogger(__name__)


def parse_usdt_from_list(payload: dict) -> Tuple[Optional[float], Optional[float]]:
    """从 /stablecoins 列表取 USDT (id=1) 的日变化."""
    assets = []
    if isinstance(payload, dict):
        assets = payload.get("peggedAssets") or []
    if not isinstance(assets, list):
        return None, None
    for c in assets:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "").upper()
        cid = str(c.get("id") or "")
        name = str(c.get("name") or "").lower()
        if sym != "USDT" and cid != "1" and "tether" not in name:
            continue
        circ = c.get("circulating") or {}
        prev = c.get("circulatingPrevDay") or {}
        try:
            latest = float(circ.get("peggedUSD")) if circ.get("peggedUSD") is not None else None
        except (TypeError, ValueError):
            latest = None
        try:
            prev_v = float(prev.get("peggedUSD")) if prev.get("peggedUSD") is not None else None
        except (TypeError, ValueError):
            prev_v = None
        net = None
        if latest is not None and prev_v is not None:
            net = latest - prev_v
        return net, latest
    return None, None


def parse_usdt_net_change(payload: dict) -> Tuple[Optional[float], Optional[float]]:
    """兼容单币详情 / 列表两种 payload."""
    if not isinstance(payload, dict):
        return None, None
    if "peggedAssets" in payload:
        return parse_usdt_from_list(payload)

    circ = payload.get("circulating")
    if isinstance(circ, dict):
        pegged = circ.get("peggedUSD")
        try:
            latest = float(pegged) if pegged is not None else None
        except (TypeError, ValueError):
            latest = None
    else:
        latest = None

    series = payload.get("tokensCirculating") or payload.get("chainBalances")
    values: list = []
    if isinstance(series, list):
        for row in series:
            if not isinstance(row, dict):
                continue
            c = row.get("circulating") or row.get("totalCirculating")
            if isinstance(c, dict):
                v = c.get("peggedUSD")
            else:
                v = c
            try:
                if v is not None:
                    values.append(float(v))
            except (TypeError, ValueError):
                continue
    elif isinstance(series, dict):
        for chain_data in series.values():
            if not isinstance(chain_data, dict):
                continue
            tokens = chain_data.get("tokens") or []
            for row in tokens:
                c = row.get("circulating") if isinstance(row, dict) else None
                if isinstance(c, dict):
                    v = c.get("peggedUSD")
                else:
                    v = c
                try:
                    if v is not None:
                        values.append(float(v))
                except (TypeError, ValueError):
                    continue

    if latest is None and values:
        latest = values[-1]

    net = None
    if len(values) >= 2:
        net = values[-1] - values[-2]
    elif latest is not None:
        prev = payload.get("circulatingPrevDay")
        if isinstance(prev, dict) and prev.get("peggedUSD") is not None:
            try:
                net = latest - float(prev["peggedUSD"])
            except (TypeError, ValueError):
                net = None

    return net, latest


class DefiLlamaCollector:
    """USDT 市值变化采集."""

    def __init__(
        self,
        poll_sec: float = DEFI_LLAMA_POLL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.poll_sec = poll_sec
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None  # lazy: asyncio.Lock 需在事件循环内创建
        self.usdt_net_24h: Optional[float] = None
        self.usdt_mcap: Optional[float] = None
        self.available = False
        self.last_success_ts: Optional[int] = None
        self.last_error: Optional[str] = None
        self._running = False

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def fetch_once(self) -> Tuple[Optional[float], Optional[float]]:
        session = await self._ensure_session()
        try:
            async with session.get(DEFI_LLAMA_LIST_URL, timeout=20) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    self.last_error = f"HTTP {resp.status}: {text[:120]}"
                    return self.usdt_net_24h, self.usdt_mcap
                payload = await resp.json()
        except Exception as exc:
            self.last_error = str(exc) or repr(exc)
            logger.warning("defi_llama error: %s", self.last_error)
            return self.usdt_net_24h, self.usdt_mcap

        net, mcap = parse_usdt_net_change(payload)
        if net is not None or mcap is not None:
            self.usdt_net_24h = net
            self.usdt_mcap = mcap
            self.available = True
            self.last_success_ts = int(time.time() * 1000)
            self.last_error = None
        else:
            self.last_error = "parse failed"
        return self.usdt_net_24h, self.usdt_mcap

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            try:
                await self.fetch_once()
            except Exception as exc:
                logger.warning("defi_llama poll failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._external_session is None and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
