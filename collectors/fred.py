"""FRED 宏观数据 — 免费 CSV 下载 (无需 API key).

序列:
  CPIAUCSL  → CPI 同比
  FEDFUNDS  → 联邦基金利率
  T10Y2Y    → 10Y-2Y 利差
    M2SL      → M2 同比
    INDPRO    → 工业产出同比 (ISM PMI 已从 FRED 下架, 作免费代理, 存入 pmi 字段)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import date, timedelta
from typing import Any, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import FRED_CSV_BASE, FRED_POLL_SEC
from models.snapshots import MacroSnapshot

logger = logging.getLogger(__name__)

SERIES = {
    "CPIAUCSL": "cpi",
    "FEDFUNDS": "fed_funds",
    "T10Y2Y": "yield_curve",
    "M2SL": "m2",
    "INDPRO": "indpro",
}


def parse_fred_csv(text: str) -> List[Tuple[str, float]]:
    """解析 FRED CSV → [(date_str, value), ...] 升序, 跳过缺失."""
    rows: List[Tuple[str, float]] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        keys = list(row.keys())
        if len(keys) < 2:
            continue
        d = row[keys[0]].strip()
        v_raw = row[keys[1]].strip()
        if not d or v_raw in ("", ".", "NA"):
            continue
        try:
            rows.append((d, float(v_raw)))
        except ValueError:
            continue
    return rows


def cpi_yoy_from_levels(levels: List[Tuple[str, float]]) -> Tuple[Optional[float], Optional[float]]:
    """月度水平 → (最新同比, 同比变化差)."""
    if len(levels) < 13:
        return None, None
    yoys: List[float] = []
    for i in range(12, len(levels)):
        prev = levels[i - 12][1]
        cur = levels[i][1]
        if prev == 0:
            continue
        yoys.append((cur - prev) / prev)
    if not yoys:
        return None, None
    latest = yoys[-1]
    change = yoys[-1] - yoys[-2] if len(yoys) >= 2 else None
    return latest, change


def yoy_from_levels(levels: List[Tuple[str, float]]) -> Optional[float]:
    yoy, _ = cpi_yoy_from_levels(levels)
    return yoy


class FredCollector:
    def __init__(
        self,
        poll_sec: float = FRED_POLL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.poll_sec = poll_sec
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None
        self._snapshot = MacroSnapshot()
        self._running = False

    def get_snapshot(self) -> MacroSnapshot:
        s = self._snapshot
        return MacroSnapshot(
            cpi_yoy=s.cpi_yoy,
            cpi_yoy_change=s.cpi_yoy_change,
            yield_curve_10y2y=s.yield_curve_10y2y,
            fed_funds_rate=s.fed_funds_rate,
            m2_yoy=s.m2_yoy,
            pmi=s.pmi,
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

    async def _fetch_series(self, series_id: str) -> List[Tuple[str, float]]:
        session = await self._ensure_session()
        start = (date.today() - timedelta(days=800)).isoformat()
        url = f"{FRED_CSV_BASE}?id={series_id}&cosd={start}"
        async with session.get(url, timeout=20) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"{series_id} HTTP {resp.status}: {text[:80]}")
            text = await resp.text()
        return parse_fred_csv(text)

    async def _safe_series(self, series_id: str) -> List[Tuple[str, float]]:
        try:
            return await self._fetch_series(series_id)
        except Exception as exc:
            logger.info("fred series %s failed: %s", series_id, exc)
            return []

    async def fetch_once(self) -> MacroSnapshot:
        snap = MacroSnapshot()
        try:
            cpi_rows, fed_rows, curve_rows, m2_rows, indpro_rows = await asyncio.gather(
                self._safe_series("CPIAUCSL"),
                self._safe_series("FEDFUNDS"),
                self._safe_series("T10Y2Y"),
                self._safe_series("M2SL"),
                self._safe_series("INDPRO"),
            )
            yoy, yoy_chg = cpi_yoy_from_levels(cpi_rows)
            snap.cpi_yoy = yoy
            snap.cpi_yoy_change = yoy_chg
            if fed_rows:
                snap.fed_funds_rate = fed_rows[-1][1]
            if curve_rows:
                snap.yield_curve_10y2y = curve_rows[-1][1]
            snap.m2_yoy = yoy_from_levels(m2_rows)
            # pmi 字段存 INDPRO 同比 (免费 PMI 代理)
            snap.pmi = yoy_from_levels(indpro_rows)

            if any(
                v is not None
                for v in (
                    snap.cpi_yoy,
                    snap.cpi_yoy_change,
                    snap.yield_curve_10y2y,
                    snap.fed_funds_rate,
                    snap.m2_yoy,
                    snap.pmi,
                )
            ):
                snap.available = True
                snap.last_success_ts = int(time.time() * 1000)
            else:
                snap.last_error = "empty series"
        except Exception as exc:
            snap.last_error = str(exc)
            logger.warning("fred error: %s", exc)
            if self._snapshot.available:
                return self.get_snapshot()

        if snap.available:
            self._snapshot = snap
        elif snap.last_error:
            self._snapshot.last_error = snap.last_error
        return self.get_snapshot()

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            try:
                await self.fetch_once()
            except Exception as exc:
                logger.warning("fred poll failed: %s", exc)
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
