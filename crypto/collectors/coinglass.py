"""CoinGlass Open API v4 采集器.

获取:
  - 全网聚合 OI 及 5m / 24h 变化
  - 清算热力图 (1~3% 磁铁带)
  - 5 分钟多空爆仓额
  - 全局账户多空比

无 COINGLASS_API_KEY 时全部字段保持 None, 不阻断 Binance 采集。
单元测试走本地 JSON 夹具, 不打真实网络。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    BLACK_SWAN_LIQ_5M_USD,
    COINGLASS_BASE_URL,
    COINGLASS_POLL_INTERVAL_SEC,
    COINGLASS_TIMEOUT_SEC,
    HEATMAP_BAND_HIGH_PCT,
    HEATMAP_BAND_LOW_PCT,
)
from models.snapshots import CoinGlassSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 纯函数解析器 (夹具可测)
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_oi_history(payload: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """从 OI aggregated-history 响应提取 (latest_oi, change_5m_pct, change_24h_pct).

    兼容多种常见字段命名。
    """
    data = payload.get("data", payload)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    rows: List[dict] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # 可能是 {time_list, open_interest_list} 或 {list: [...]}
        if "list" in data and isinstance(data["list"], list):
            rows = data["list"]
        elif "open_interest_list" in data and "time_list" in data:
            times = data["time_list"]
            ois = data["open_interest_list"]
            # close 优先, 否则取 list 本身
            if isinstance(ois, dict):
                closes = ois.get("c") or ois.get("close") or ois.get("o")
            else:
                closes = ois
            if closes and times and len(closes) == len(times):
                rows = [
                    {"t": times[i], "oi": closes[i]}
                    for i in range(len(times))
                ]

    if not rows:
        return None, None, None

    def _row_oi(row: dict) -> Optional[float]:
        for key in ("oi", "open_interest", "openInterest", "c", "close", "o"):
            if key in row:
                return _safe_float(row[key])
        return None

    def _row_ts(row: dict) -> Optional[int]:
        for key in ("t", "time", "timestamp", "createTime"):
            if key in row:
                v = row[key]
                try:
                    ts = int(v)
                    # 秒 → 毫秒
                    if ts < 1_000_000_000_000:
                        ts *= 1000
                    return ts
                except (TypeError, ValueError):
                    return None
        return None

    parsed = [( _row_ts(r), _row_oi(r) ) for r in rows]
    parsed = [(t, o) for t, o in parsed if o is not None]
    if not parsed:
        return None, None, None

    # 按时间排序
    parsed.sort(key=lambda x: x[0] or 0)
    latest_ts, latest_oi = parsed[-1]
    if latest_oi is None:
        return None, None, None

    def _pct_since(ms_ago: int) -> Optional[float]:
        if latest_ts is None:
            # 无时间戳时用索引近似: 假设末尾是最新, interval 已知时由调用方保证
            return None
        target = latest_ts - ms_ago
        # 找最接近 target 且 <= latest 的点
        candidate = None
        for t, o in parsed:
            if t is not None and t <= target:
                candidate = o
        if candidate is None or candidate == 0:
            # 回退: 取最早点
            if len(parsed) >= 2 and parsed[0][1]:
                candidate = parsed[0][1]
            else:
                return None
        if candidate == 0:
            return None
        return (latest_oi - candidate) / candidate

    # 若有时间戳用时间窗; 否则用列表位置近似 (5m≈最后几根, 24h≈更早)
    ch_5m = _pct_since(5 * 60 * 1000)
    ch_24h = _pct_since(24 * 60 * 60 * 1000)

    if ch_5m is None and len(parsed) >= 2:
        prev = parsed[-2][1]
        if prev and prev != 0:
            ch_5m = (latest_oi - prev) / prev

    if ch_24h is None and len(parsed) >= 2:
        first = parsed[0][1]
        if first and first != 0:
            ch_24h = (latest_oi - first) / first

    return latest_oi, ch_5m, ch_24h


def parse_liquidation_heatmap(
    payload: dict,
    mark_price: float,
    band_low: float = HEATMAP_BAND_LOW_PCT,
    band_high: float = HEATMAP_BAND_HIGH_PCT,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """解析清算热力图 → (above_intensity, below_intensity, magnet).

    magnet ∈ [-1, +1]: +1 上方空头密集 (做多磁铁), -1 下方多头密集 (做空磁铁).
    """
    if mark_price <= 0:
        return None, None, None

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None, None, None

    y_axis = data.get("y_axis") or data.get("price_levels") or []
    liq_data = (
        data.get("liquidation_leverage_data")
        or data.get("data")
        or data.get("liquidations")
        or []
    )
    if not y_axis or not liq_data:
        return None, None, None

    prices = [float(p) for p in y_axis]
    # 每个价位累计清算杠杆
    level_totals: Dict[int, float] = {}
    for item in liq_data:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        _x, y_idx, leverage = item[0], int(item[1]), float(item[2])
        if 0 <= y_idx < len(prices):
            level_totals[y_idx] = level_totals.get(y_idx, 0.0) + abs(leverage)

    if not level_totals:
        return None, None, None

    above = 0.0
    below = 0.0
    for idx, total in level_totals.items():
        price = prices[idx]
        dist = (price - mark_price) / mark_price
        abs_dist = abs(dist)
        if band_low <= abs_dist <= band_high:
            if dist > 0:
                above += total
            else:
                below += total

    total = above + below
    if total <= 0:
        return 0.0, 0.0, 0.0

    above_i = above / total
    below_i = below / total
    # magnet: 上方主导 → 正, 下方主导 → 负
    magnet = above_i - below_i
    return above_i, below_i, magnet


def parse_liquidation_history(payload: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """解析爆仓历史 → (long_5m_usd, short_5m_usd, total_5m_usd).

    若返回的是聚合列表, 累加最近窗口; 若是单点 coin-list, 直接读字段.
    """
    data = payload.get("data", payload)

    # coin-list / 单对象
    if isinstance(data, dict) and not isinstance(data.get("list"), list):
        long_v = _safe_float(
            data.get("long_liquidation_usd")
            or data.get("longLiquidation_usd")
            or data.get("longVolUsd")
            or data.get("h1_long_liquidation_usd")
        )
        short_v = _safe_float(
            data.get("short_liquidation_usd")
            or data.get("shortLiquidation_usd")
            or data.get("shortVolUsd")
            or data.get("h1_short_liquidation_usd")
        )
        if long_v is not None or short_v is not None:
            long_v = long_v or 0.0
            short_v = short_v or 0.0
            return long_v, short_v, long_v + short_v

    rows: List[dict] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("list") or []

    if not rows:
        return None, None, None

    # 取最后一根 (最近 interval) 作为 5m 近似; 若有多根且带时间, 累加 5 分钟内
    def _long(row: dict) -> float:
        return _safe_float(
            row.get("long_liquidation_usd")
            or row.get("longVolUsd")
            or row.get("long")
            or row.get("buyVolUsd"),
            0.0,
        ) or 0.0

    def _short(row: dict) -> float:
        return _safe_float(
            row.get("short_liquidation_usd")
            or row.get("shortVolUsd")
            or row.get("short")
            or row.get("sellVolUsd"),
            0.0,
        ) or 0.0

    last = rows[-1]
    long_v = _long(last)
    short_v = _short(last)
    return long_v, short_v, long_v + short_v


def parse_long_short_ratio(payload: dict) -> Optional[float]:
    """解析全局账户多空比 (long/short)."""
    data = payload.get("data", payload)
    rows: List[dict] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        if "list" in data:
            rows = data["list"]
        else:
            # 单点
            for key in ("long_short_ratio", "longShortRatio", "ratio", "global_account_long_short_ratio"):
                if key in data:
                    return _safe_float(data[key])
            # longAccount / shortAccount
            la = _safe_float(data.get("longAccount") or data.get("long_account"))
            sa = _safe_float(data.get("shortAccount") or data.get("short_account"))
            if la is not None and sa is not None and sa > 0:
                return la / sa
            return None

    if not rows:
        return None
    last = rows[-1]
    for key in ("long_short_ratio", "longShortRatio", "ratio"):
        if key in last:
            return _safe_float(last[key])
    la = _safe_float(last.get("longAccount") or last.get("long_account"))
    sa = _safe_float(last.get("shortAccount") or last.get("short_account"))
    if la is not None and sa is not None and sa > 0:
        return la / sa
    return None


def build_coinglass_snapshot(
    oi_payload: Optional[dict] = None,
    heatmap_payload: Optional[dict] = None,
    liq_payload: Optional[dict] = None,
    lsr_payload: Optional[dict] = None,
    mark_price: Optional[float] = None,
) -> CoinGlassSnapshot:
    """将各端点原始 JSON 组装为 CoinGlassSnapshot."""
    snap = CoinGlassSnapshot()
    got_any = False

    if oi_payload is not None:
        oi, ch5, ch24 = parse_oi_history(oi_payload)
        snap.open_interest_usd = oi
        snap.oi_change_5m_pct = ch5
        snap.oi_change_24h_pct = ch24
        if oi is not None:
            got_any = True

    if heatmap_payload is not None and mark_price:
        above, below, magnet = parse_liquidation_heatmap(heatmap_payload, mark_price)
        snap.heatmap_above_intensity = above
        snap.heatmap_below_intensity = below
        snap.heatmap_magnet = magnet
        if magnet is not None:
            got_any = True

    if liq_payload is not None:
        long_v, short_v, total = parse_liquidation_history(liq_payload)
        snap.liq_long_5m_usd = long_v
        snap.liq_short_5m_usd = short_v
        snap.liq_total_5m_usd = total
        if long_v is None and short_v is None:
            snap.liq_window_status = "error"
        elif long_v is None or short_v is None:
            snap.liq_window_status = "partial"
        else:
            snap.liq_window_status = "complete"
        if total is not None:
            got_any = True
    else:
        snap.liq_window_status = "missing"

    if lsr_payload is not None:
        snap.long_short_ratio = parse_long_short_ratio(lsr_payload)
        if snap.long_short_ratio is not None:
            got_any = True

    snap.available = got_any
    return snap


class CoinGlassCollector:
    """CoinGlass REST 轮询采集器. 无 API Key 时返回空快照."""

    PLACEHOLDER_KEYS = {
        "",
        "你的key",
        "这里粘贴真实的key",
        "YOUR_API_KEY_HERE",
        "YOUR_KEY",
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = COINGLASS_BASE_URL,
        poll_interval: float = COINGLASS_POLL_INTERVAL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        raw = api_key if api_key is not None else os.environ.get("COINGLASS_API_KEY", "")
        self.api_key = (raw or "").strip()
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self._external_session = session
        self._session: Optional[Any] = None
        self._snapshot = CoinGlassSnapshot()
        self._running = False
        self._mark_price: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_http_status: Optional[int] = None

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key) and self.api_key not in self.PLACEHOLDER_KEYS

    @property
    def key_status(self) -> str:
        if not self.api_key:
            return "missing"
        if self.api_key in self.PLACEHOLDER_KEYS:
            return "placeholder"
        return f"set(len={len(self.api_key)})"

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
            liq_window_status=getattr(s, "liq_window_status", None),
            long_short_ratio=s.long_short_ratio,
            liquidation_speed_long_cleared=s.liquidation_speed_long_cleared,
            liquidation_speed_short_cleared=s.liquidation_speed_short_cleared,
            available=s.available,
        )

    def set_mark_price(self, price: float) -> None:
        self._mark_price = price

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for CoinGlassCollector")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self.has_api_key:
            self.last_error = "no valid API key"
            return None
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = {"accept": "application/json", "CG-API-KEY": self.api_key}
        try:
            async with session.get(
                url,
                params=params or {},
                headers=headers,
                timeout=COINGLASS_TIMEOUT_SEC,
            ) as resp:
                self.last_http_status = resp.status
                if resp.status >= 400:
                    text = await resp.text()
                    self.last_error = f"HTTP {resp.status}: {text[:200]}"
                    logger.warning("CoinGlass %s → %s", path, self.last_error)
                    return None
                data = await resp.json()
                # CoinGlass 业务错误码
                code = str(data.get("code", "0")) if isinstance(data, dict) else "0"
                if code not in ("0", "200", ""):
                    self.last_error = f"API code={code} msg={data.get('msg')}"
                    logger.warning("CoinGlass %s → %s", path, self.last_error)
                    return None
                self.last_error = None
                return data
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("CoinGlass %s error: %s", path, exc)
            return None

    async def fetch_once(self, mark_price: Optional[float] = None) -> CoinGlassSnapshot:
        """拉一轮数据并更新内部快照. 无 key 时返回空快照."""
        if not self.has_api_key:
            self._snapshot = CoinGlassSnapshot(available=False)
            return self.get_snapshot()

        price = mark_price or self._mark_price

        oi_payload, heatmap_payload, liq_payload, lsr_payload = await asyncio.gather(
            self._get(
                "/api/futures/open-interest/aggregated-history",
                {"symbol": "BTC", "interval": "5m", "limit": 300},
            ),
            self._get(
                "/api/futures/liquidation/aggregated-heatmap/model1",
                {"symbol": "BTC", "range": "24h"},
            ),
            self._get(
                "/api/futures/liquidation/aggregated-history",
                {"symbol": "BTC", "interval": "5m", "limit": 12},
            ),
            self._get(
                "/api/futures/global-long-short-account-ratio/history",
                {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "5m", "limit": 12},
            ),
        )

        self._snapshot = build_coinglass_snapshot(
            oi_payload=oi_payload,
            heatmap_payload=heatmap_payload if price else None,
            liq_payload=liq_payload,
            lsr_payload=lsr_payload,
            mark_price=price,
        )
        return self.get_snapshot()

    async def run(
        self,
        stop_event: Optional[asyncio.Event] = None,
        mark_price_provider: Optional[Any] = None,
    ) -> None:
        """轮询主循环."""
        self._running = True
        stop = stop_event or asyncio.Event()

        if not self.has_api_key:
            logger.warning("COINGLASS_API_KEY not set — CoinGlass collector idle")
            while self._running and not stop.is_set():
                await asyncio.sleep(self.poll_interval)
            return

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

        await self.close()

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


def is_black_swan_liquidation(snap: CoinGlassSnapshot) -> bool:
    total = snap.liq_total_5m_usd
    return total is not None and total >= BLACK_SWAN_LIQ_5M_USD
