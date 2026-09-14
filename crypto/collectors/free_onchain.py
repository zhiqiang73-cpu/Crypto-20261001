"""免费链上代理采集 — hashrate / 巨鲸 / USDT 铸销 / MVRV~ / 交易所存量~.

V8 新增粗略代理 (面板标 ~):
  - mvrv_approx: blockchain.info 市值 / (365日均价 × 流通量)
  - exchange_reserves_proxy: Binance 24h 买卖压力代理 → [-1, +1]

仍常驻 None (无可靠免费源):
  NUPL / LTH / SOPR / miner_reserves
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    MEMPOOL_HASHRATE_URL,
    ONCHAIN_POLL_SEC,
    WHALE_ALERT_FREE_URL,
    WHALE_EXCHANGE_BTC_THRESHOLD,
    WHALE_MIN_USD,
)
from models.snapshots import OnchainSnapshot

logger = logging.getLogger(__name__)

DEFILLAMA_USDT_URL = "https://stablecoins.llama.fi/stablecoin/1"  # Tether
WHALE_ALERT_WEB = "https://whale-alert.io/"
BLOCKCHAIN_MARKETCAP_URL = "https://blockchain.info/q/marketcap"
BLOCKCHAIN_TOTALBC_URL = "https://blockchain.info/q/totalbc"
BINANCE_TICKER_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def parse_mempool_hashrate(payload: dict) -> Optional[float]:
    """mempool /api/v1/mining/hashrate/1m → 30日均线变化率近似.

    返回 (最新 - 约30日前) / 30日前.
    """
    series = payload.get("hashrates") or payload.get("difficulty") or []
    if not isinstance(series, list) or len(series) < 2:
        # 有些版本直接给 currentHashrate / hashRate
        cur = payload.get("currentHashrate") or payload.get("hashRate")
        avg = payload.get("currentDifficulty")
        if cur and avg:
            try:
                return 0.0  # 单点无法算变化
            except (TypeError, ValueError):
                return None
        return None

    vals: List[Tuple[int, float]] = []
    for row in series:
        if isinstance(row, dict):
            ts = int(row.get("timestamp") or row.get("t") or 0)
            hr = float(row.get("avgHashrate") or row.get("hashrate") or 0)
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            ts, hr = int(row[0]), float(row[1])
        else:
            continue
        if hr > 0:
            vals.append((ts, hr))
    if len(vals) < 2:
        return None
    vals.sort(key=lambda x: x[0])
    latest_ts, latest = vals[-1]
    target = latest_ts - 30 * 86400
    old = vals[0][1]
    for ts, hr in vals:
        if ts <= target:
            old = hr
    if old <= 0:
        return None
    return (latest - old) / old


def classify_whale_flow(
    to_exchange_btc: float,
    from_exchange_btc: float,
    threshold_btc: float = WHALE_EXCHANGE_BTC_THRESHOLD,
) -> Tuple[Optional[float], str]:
    """返回 (net_flow_btc, direction).

    net > 0 = 流入交易所 (看空); < 0 = 流出 (看多).
    """
    net = to_exchange_btc - from_exchange_btc
    if abs(net) < threshold_btc:
        return net, "none"
    if net > 0:
        return net, "to_exchange"
    return net, "from_exchange"


def parse_whale_alert_api(payload: dict) -> Tuple[float, float]:
    """Whale Alert API → (to_exchange_btc, from_exchange_btc)."""
    txs = payload.get("transactions") or []
    to_ex = 0.0
    from_ex = 0.0
    for tx in txs:
        if (tx.get("symbol") or "").lower() not in ("btc", "bitcoin"):
            continue
        amount = float(tx.get("amount") or 0)
        if amount <= 0:
            continue
        from_owner = ((tx.get("from") or {}).get("owner_type") or "").lower()
        to_owner = ((tx.get("to") or {}).get("owner_type") or "").lower()
        if to_owner == "exchange" and from_owner != "exchange":
            to_ex += amount
        elif from_owner == "exchange" and to_owner != "exchange":
            from_ex += amount
    return to_ex, from_ex


def parse_whale_alert_html(html: str) -> Tuple[float, float]:
    """无 API key 时的粗解析 — 只认含 bitcoin + exchange 的行."""
    to_ex = 0.0
    from_ex = 0.0
    # 例: "1234 BTC transferred from unknown wallet to Binance"
    pattern = re.compile(
        r"([\d,.]+)\s*BTC\s+(?:transferred|moved)\s+from\s+(.+?)\s+to\s+(.+?)(?:<|$)",
        re.I,
    )
    exchanges = (
        "binance", "coinbase", "kraken", "okx", "bitfinex", "bybit",
        "huobi", "gemini", "bitstamp", "exchange",
    )
    for m in pattern.finditer(html):
        try:
            amt = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        src = m.group(2).lower()
        dst = m.group(3).lower()
        src_ex = any(e in src for e in exchanges)
        dst_ex = any(e in dst for e in exchanges)
        if dst_ex and not src_ex:
            to_ex += amt
        elif src_ex and not dst_ex:
            from_ex += amt
    return to_ex, from_ex


def parse_defillama_usdt(payload: dict) -> Tuple[Optional[float], Optional[float]]:
    """用市值日变化近似 mint/burn.

    返回 (mint_24h, burn_24h); 上涨记 mint, 下跌记 burn.
    """
    try:
        tokens = payload.get("tokens") or []
        if not tokens:
            # 某些端点直接给 circulatingPrevDay
            cur = float(payload.get("circulating") or payload.get("circulatingUSD") or 0)
            prev = float(
                payload.get("circulatingPrevDay")
                or payload.get("circulatingUSDPrevDay")
                or 0
            )
            if cur and prev:
                delta = cur - prev
                if delta >= 0:
                    return delta, 0.0
                return 0.0, abs(delta)
            return None, None
        # tokens: [{date, circulating, ...}]
        pts = []
        for t in tokens:
            d = t.get("date") or t.get("timestamp")
            circ = t.get("circulating") or t.get("circulatingUSD")
            if d is None or circ is None:
                continue
            if isinstance(circ, dict):
                circ = circ.get("peggedUSD") or circ.get("USD") or 0
            pts.append((int(d), float(circ)))
        if len(pts) < 2:
            return None, None
        pts.sort(key=lambda x: x[0])
        delta = pts[-1][1] - pts[-2][1]
        if delta >= 0:
            return delta, 0.0
        return 0.0, abs(delta)
    except (TypeError, ValueError, KeyError):
        return None, None


class FreeOnchainCollector:
    def __init__(self, poll_sec: float = ONCHAIN_POLL_SEC) -> None:
        self.poll_sec = poll_sec
        self._session: Optional[Any] = None
        self.snapshot = OnchainSnapshot()
        self._running = False
        self.whale_api_key = os.environ.get("WHALE_ALERT_API_KEY", "").strip()
        self._mvrv_cache: Optional[float] = None
        self._mvrv_cache_ts: float = 0.0
        self._mvrv_ttl_sec = 6 * 3600.0  # MVRV 日级变化, 6h 缓存


    @property
    def last_success_ts(self) -> Optional[int]:
        return self.snapshot.last_success_ts

    def get_snapshot(self) -> OnchainSnapshot:
        return self.snapshot

    async def _session_get(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                headers={"User-Agent": "btc-four-face/1.0"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_hashrate(self) -> Optional[float]:
        sess = await self._session_get()
        async with sess.get(MEMPOOL_HASHRATE_URL) as resp:
            if resp.status != 200:
                raise RuntimeError(f"mempool {resp.status}")
            data = await resp.json()
        return parse_mempool_hashrate(data)

    async def fetch_whales(self) -> Tuple[float, float]:
        sess = await self._session_get()
        if self.whale_api_key:
            params = {
                "api_key": self.whale_api_key,
                "min_value": int(WHALE_MIN_USD),
                "start": int(time.time()) - 3600,
            }
            async with sess.get(WHALE_ALERT_FREE_URL, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return parse_whale_alert_api(data)
                logger.info("whale-alert api %s, fallback html", resp.status)
        async with sess.get(WHALE_ALERT_WEB) as resp:
            if resp.status != 200:
                return 0.0, 0.0
            html = await resp.text()
        return parse_whale_alert_html(html)

    async def fetch_usdt(self) -> Tuple[Optional[float], Optional[float]]:
        sess = await self._session_get()
        async with sess.get(DEFILLAMA_USDT_URL) as resp:
            if resp.status != 200:
                raise RuntimeError(f"defillama {resp.status}")
            data = await resp.json()
        return parse_defillama_usdt(data)

    async def fetch_once(self) -> OnchainSnapshot:
        snap = OnchainSnapshot()
        errors = []
        try:
            snap.hashrate_ma30_change_pct = await self.fetch_hashrate()
        except Exception as exc:
            errors.append(f"hashrate:{exc}")
            logger.warning("hashrate: %s", exc)

        try:
            to_ex, from_ex = await self.fetch_whales()
            net, direction = classify_whale_flow(to_ex, from_ex)
            snap.whale_net_flow_btc = net
            snap.whale_transfer_direction = direction
            # USD 粗算用阈值名义
            if net is not None:
                snap.whale_net_flow_usd = None  # 无可靠现价时留空, live_loop 可补
        except Exception as exc:
            errors.append(f"whale:{exc}")
            logger.warning("whale: %s", exc)

        try:
            mint, burn = await self.fetch_usdt()
            snap.usdt_mint_24h = mint
            snap.usdt_burn_24h = burn
        except Exception as exc:
            errors.append(f"usdt:{exc}")
            logger.warning("usdt: %s", exc)

        try:
            snap.mvrv_approx = await self.fetch_mvrv_approx()
        except Exception as exc:
            errors.append(f"mvrv:{exc}")
            logger.warning("mvrv: %s", exc)

        try:
            snap.exchange_reserves_proxy = await self.fetch_exchange_reserves_proxy()
        except Exception as exc:
            errors.append(f"ex_reserves:{exc}")
            logger.warning("ex_reserves: %s", exc)

        snap.available = any(
            v is not None
            for v in (
                snap.hashrate_ma30_change_pct,
                snap.whale_net_flow_btc,
                snap.usdt_mint_24h,
                snap.usdt_burn_24h,
                snap.mvrv_approx,
                snap.exchange_reserves_proxy,
            )
        )
        if snap.available:
            snap.last_success_ts = int(time.time() * 1000)
        if errors:
            snap.last_error = "; ".join(errors)
        self.snapshot = snap
        return snap

    async def fetch_mvrv_approx(self) -> Optional[float]:
        """粗略 MVRV ≈ market_cap / (avg_365d_price × circulating).

        realized_value 用 365 日均价 × 流通量近似; 面板应标 ~.
        6h 缓存, 避免每次轮询拉 365 根日 K 卡住评分环.
        """
        now = time.time()
        if (
            self._mvrv_cache is not None
            and (now - self._mvrv_cache_ts) < self._mvrv_ttl_sec
        ):
            return self._mvrv_cache

        session = await self._session_get()
        async with session.get(BLOCKCHAIN_MARKETCAP_URL, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"marketcap {resp.status}")
            market_cap = float((await resp.text()).strip())
        async with session.get(BLOCKCHAIN_TOTALBC_URL, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"totalbc {resp.status}")
            total_sats = float((await resp.text()).strip())
            circulating = total_sats / 1e8
        async with session.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 365},
            timeout=15,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"klines {resp.status}")
            rows = await resp.json()
        if not isinstance(rows, list) or len(rows) < 30:
            return self._mvrv_cache
        closes = [float(r[4]) for r in rows if isinstance(r, list) and len(r) > 4]
        if not closes:
            return self._mvrv_cache
        avg_price = sum(closes) / len(closes)
        realized_approx = avg_price * circulating
        if realized_approx <= 0 or market_cap <= 0:
            return self._mvrv_cache
        val = round(market_cap / realized_approx, 4)
        self._mvrv_cache = val
        self._mvrv_cache_ts = now
        return val

    async def fetch_exchange_reserves_proxy(self) -> Optional[float]:
        """Binance 24h ticker 买卖压力代理 → [-1, +1].

        卖压强 (价跌+量大) → 负 (流入交易所近似);
        买压强 → 正 (流出交易所近似).
        """
        session = await self._session_get()
        async with session.get(
            BINANCE_TICKER_24H,
            params={"symbol": "BTCUSDT"},
            timeout=15,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ticker {resp.status}")
            data = await resp.json()
        try:
            change_pct = float(data.get("priceChangePercent") or 0) / 100.0
            quote_vol = float(data.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            return None
        # 归一: 日波动 ±5% 映射到 ±1, 再按成交量强度略放大
        vol_factor = min(1.5, max(0.5, quote_vol / 5_000_000_000))  # ~50亿 USDT 为常态
        raw = change_pct / 0.05 * vol_factor
        return round(max(-1.0, min(1.0, raw)), 4)

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            await self.fetch_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        await self.close()

    def stop(self) -> None:
        self._running = False
