"""Crypto Fear & Greed Index — alternative.me 免费 API."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import FEAR_GREED_POLL_SEC, FEAR_GREED_URL
from models.snapshots import FearGreedSnapshot

logger = logging.getLogger(__name__)


def parse_fear_greed(payload: dict) -> Optional[FearGreedSnapshot]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data or not isinstance(data, list):
        return None
    row = data[0]
    try:
        value = float(row.get("value"))
    except (TypeError, ValueError):
        return None
    return FearGreedSnapshot(
        value=value,
        classification=str(row.get("value_classification") or "") or None,
        available=True,
        last_success_ts=int(time.time() * 1000),
    )


class FearGreedCollector:
    def __init__(
        self,
        poll_sec: float = FEAR_GREED_POLL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.poll_sec = poll_sec
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None  # lazy: asyncio.Lock 需在事件循环内创建
        self._snapshot = FearGreedSnapshot()
        self._running = False

    def get_snapshot(self) -> FearGreedSnapshot:
        s = self._snapshot
        return FearGreedSnapshot(
            value=s.value,
            classification=s.classification,
            available=s.available,
            last_success_ts=s.last_success_ts,
            last_error=s.last_error,
        )

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

    async def fetch_once(self) -> FearGreedSnapshot:
        session = await self._ensure_session()
        try:
            async with session.get(FEAR_GREED_URL, timeout=15) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    self._snapshot.last_error = f"HTTP {resp.status}: {text[:120]}"
                    return self.get_snapshot()
                payload = await resp.json()
        except Exception as exc:
            self._snapshot.last_error = str(exc)
            logger.warning("fear_greed error: %s", exc)
            return self.get_snapshot()

        parsed = parse_fear_greed(payload)
        if parsed:
            self._snapshot = parsed
        else:
            self._snapshot.last_error = "parse failed"
        return self.get_snapshot()

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            try:
                await self.fetch_once()
            except Exception as exc:
                logger.warning("fear_greed poll failed: %s", exc)
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
