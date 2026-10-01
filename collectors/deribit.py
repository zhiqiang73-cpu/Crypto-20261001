"""Deribit 公开 REST — Max Pain + DVOL(IV).

无需 API Key.
Max Pain: 对最近到期的 BTC 期权, 在各行使价上计算 call+put OI 的净支出,
取总支付最小的行权价.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import DERIBIT_POLL_SEC, DERIBIT_REST
from models.snapshots import DeribitSnapshot

logger = logging.getLogger(__name__)


def parse_instrument_name(name: str) -> Optional[Tuple[str, float, str]]:
    """BTC-27JUN25-100000-C → (expiry, strike, 'C'|'P')."""
    parts = name.split("-")
    if len(parts) < 4:
        return None
    try:
        strike = float(parts[2])
    except ValueError:
        return None
    side = parts[3].upper()
    if side not in ("C", "P"):
        return None
    return parts[1], strike, side


def compute_max_pain(instruments: List[dict]) -> Optional[Tuple[float, str]]:
    """从 book_summary 列表算 Max Pain.

    instruments 项需含 instrument_name / open_interest.
    返回 (max_pain_strike, expiry) 或 None.
    """
    by_expiry: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    # list of (strike, call_oi, put_oi) per expiry — we'll accumulate
    call_oi: Dict[Tuple[str, float], float] = defaultdict(float)
    put_oi: Dict[Tuple[str, float], float] = defaultdict(float)

    for row in instruments:
        name = row.get("instrument_name") or ""
        parsed = parse_instrument_name(name)
        if not parsed:
            continue
        expiry, strike, side = parsed
        oi = float(row.get("open_interest") or 0)
        if oi <= 0:
            continue
        if side == "C":
            call_oi[(expiry, strike)] += oi
        else:
            put_oi[(expiry, strike)] += oi
        by_expiry[expiry].append(strike)

    if not by_expiry:
        return None

    # 选最近到期 (Deribit 日期如 27JUN25 — 字典序不完全可靠,
    # 用总 OI 最大的到期作为流动性优先; 若只有一个就用它)
    def expiry_oi(exp: str) -> float:
        total = 0.0
        for (e, s), v in call_oi.items():
            if e == exp:
                total += v
        for (e, s), v in put_oi.items():
            if e == exp:
                total += v
        return total

    expiry = max(by_expiry.keys(), key=expiry_oi)
    strikes = sorted(set(by_expiry[expiry]))
    if not strikes:
        return None

    best_strike = None
    best_pain = None
    for candidate in strikes:
        pain = 0.0
        for s in strikes:
            c_oi = call_oi.get((expiry, s), 0.0)
            p_oi = put_oi.get((expiry, s), 0.0)
            # call 持有者在 settlement > strike 时赚 (settlement - strike)
            if candidate > s:
                pain += c_oi * (candidate - s)
            # put 持有者在 settlement < strike 时赚 (strike - settlement)
            if candidate < s:
                pain += p_oi * (s - candidate)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = candidate

    if best_strike is None:
        return None
    return best_strike, expiry


def parse_dvol(payload: dict) -> Optional[float]:
    """get_volatility_index_data 或 ticker 返回 → IV 小数."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if result is None:
        result = payload
    if isinstance(result, dict):
        # DVOL 时间序列: {"data": [[ts,o,h,l,c], ...]}
        series = result.get("data")
        if isinstance(series, list) and series:
            last = series[-1]
            if isinstance(last, (list, tuple)) and len(last) >= 5:
                try:
                    v = float(last[4])
                    return v / 100.0 if v > 1.5 else v
                except (TypeError, ValueError):
                    pass
        for key in ("volatility", "dvol", "mark_iv", "last"):
            if key in result and result[key] is not None:
                try:
                    v = float(result[key])
                    return v / 100.0 if v > 1.5 else v
                except (TypeError, ValueError):
                    continue
        if "index_price" in result:
            try:
                v = float(result["index_price"])
                return v / 100.0 if v > 1.5 else v
            except (TypeError, ValueError):
                pass
    if isinstance(result, list) and result:
        last = result[-1]
        if isinstance(last, (list, tuple)) and len(last) >= 5:
            try:
                v = float(last[4])
                return v / 100.0 if v > 1.5 else v
            except (TypeError, ValueError):
                pass
    return None


def median_mark_iv(instruments: List[dict]) -> Optional[float]:
    """从期权 book_summary 的 mark_iv (%) 取中位数 → 小数."""
    vals = []
    for row in instruments:
        iv = row.get("mark_iv")
        if iv is None:
            continue
        try:
            v = float(iv)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v / 100.0 if v > 1.5 else v)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


class DeribitCollector:
    def __init__(self, poll_sec: float = DERIBIT_POLL_SEC) -> None:
        self.poll_sec = poll_sec
        self._session: Optional[Any] = None
        self.snapshot = DeribitSnapshot()
        self._running = False

    @property
    def last_success_ts(self) -> Optional[int]:
        return self.snapshot.last_success_ts

    def get_snapshot(self) -> DeribitSnapshot:
        return self.snapshot

    async def _session_get(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        sess = await self._session_get()
        url = f"{DERIBIT_REST}/{method}"
        async with sess.get(url, params=params or {}) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"deribit {method} {resp.status}: {text[:200]}")
            return await resp.json()

    async def fetch_once(
        self, mark_price: Optional[float] = None
    ) -> DeribitSnapshot:
        snap = DeribitSnapshot()
        try:
            book = await self._rpc(
                "public/get_book_summary_by_currency",
                {"currency": "BTC", "kind": "option"},
            )
            rows = book.get("result") or []
            mp = compute_max_pain(rows)
            if mp:
                snap.max_pain, snap.expiry = mp

            # index price
            idx = await self._rpc(
                "public/get_index_price", {"index_name": "btc_usd"}
            )
            idx_price = (idx.get("result") or {}).get("index_price")
            if idx_price is not None:
                snap.mark_price = float(idx_price)
            elif mark_price is not None:
                snap.mark_price = mark_price

            if snap.max_pain and snap.mark_price:
                snap.max_pain_distance = (
                    snap.mark_price - snap.max_pain
                ) / snap.max_pain

            # DVOL
            try:
                dvol = await self._rpc(
                    "public/get_volatility_index_data",
                    {
                        "currency": "BTC",
                        "resolution": "3600",
                        "end_timestamp": int(time.time() * 1000),
                        "start_timestamp": int(time.time() * 1000) - 86_400_000,
                    },
                )
                snap.iv = parse_dvol(dvol)
            except Exception as exc:
                logger.info("deribit DVOL parse failed: %s", exc)
                snap.iv = None
            if snap.iv is None:
                snap.iv = median_mark_iv(rows)

            snap.available = snap.max_pain is not None or snap.iv is not None
            if snap.available:
                snap.last_success_ts = int(time.time() * 1000)
            else:
                snap.last_error = "empty deribit payload"
        except Exception as exc:
            logger.warning("deribit fetch failed: %s", exc)
            snap.last_error = str(exc)
            if self.snapshot.available:
                snap = self.snapshot
                snap.last_error = str(exc)

        self.snapshot = snap
        return snap

    async def run(
        self,
        stop_event: Optional[asyncio.Event] = None,
        mark_price_provider=None,
    ) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            mp = mark_price_provider() if mark_price_provider else None
            await self.fetch_once(mark_price=mp)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        await self.close()

    def stop(self) -> None:
        self._running = False
