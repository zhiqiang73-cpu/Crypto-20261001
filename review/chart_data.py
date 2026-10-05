"""图片显示确认模块 —— K 线 / KDJ / MACD / B-S 标记的数据源（只读，仅测试网）。

用途：把 bot 真正使用的那条行情序列画到面板上，并标出策略的 B/S 信号，
供人工核对「策略有没有按规则执行」。**本模块只读行情与本地成交日志，
不参与任何下单决策，也不写任何文件。**

口径（与运行器完全一致，不另起一套）：
  * 行情地址由 `config.market_endpoints.resolve_for_account()` 反推 ——
    账户在测试网，行情就必须是测试网；这是 2026-10-02 市场错位事故的硬闸门。
  * KDJ(9,3,3) 与 MACD(12,26,9) 直接加载 `shadow/indicators.py` 的同一份函数，
    避免出现「图上的 K/D」与「bot 的 K/D」两套口径。
  * 交叉与 MACD 闸门直接加载 `shadow/signals.py`：金叉且柱正 → B；
    死叉且柱负 → S；方向背离一律丢弃（不开仓、不平仓、不反手）。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

try:  # aiohttp 由面板进程提供
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore


def _load_standalone(name: str, relpath: str):
    """按文件路径加载模块，绕开 shadow 包的 __init__（它会把线上运行器带进来）。

    加载的是**同一份文件**，因此指标口径与运行器逐字一致。
    """
    path = os.path.join(ROOT, relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {relpath}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_IND = _load_standalone("crypto_chart_indicators", "shadow/indicators.py")
_SIG = _load_standalone("crypto_chart_signals", "shadow/signals.py")

# --------------------------------------------------------------------------- 固定口径
SYMBOLS = ("BTCUSDT", "ETHUSDT")
INTERVALS = ("15m", "5m")
KDJ_PARAMS = (9, 3, 3)
MACD_PARAMS = (12, 26, 9)

# 每个 (标的, 周期) 对应的策略身份 —— 取自 config/strategies/*.json 的 runtime_key。
STRATEGY_KEYS = {
    ("BTCUSDT", "15m"): "kdj15",
    ("BTCUSDT", "5m"): "kdj5",
    ("ETHUSDT", "15m"): "eth15",
    ("ETHUSDT", "5m"): "eth5",
}

SIGNAL_RULE = ("当根收盘金叉且 MACD 能量柱为正 → 做多（B）；"
               "死叉且能量柱为负 → 做空（S）；方向背离的交叉丢弃不操作；"
               "信号在下一根 K 线开盘执行。")

DEFAULT_DISPLAY_BARS = 200
FETCH_BARS = 500          # 抓取根数（含指标预热）
MAX_DISPLAY_BARS = 500
CACHE_TTL_SEC = 3.0
TRADES_CSV = os.path.join(ROOT, "runtime", "shadow", "deployed_trades.csv")

# 运行器落盘的读数快照 —— 用于逐根核对「图上的数值 = bot 真正用的数值」。
READING_FILES = {
    ("BTCUSDT", "15m"): "latest_reading.json",
    ("BTCUSDT", "5m"): "latest_reading_5m.json",
    ("ETHUSDT", "15m"): "latest_reading_eth15.json",
    ("ETHUSDT", "5m"): "latest_reading_eth5.json",
}

_CACHE: Dict[Tuple[str, str, int], Tuple[float, Dict[str, Any]]] = {}


# --------------------------------------------------------------------------- 工具
def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _beijing_to_ms(text: str) -> Optional[int]:
    """把日志里的北京时间 "YYYY-MM-DD HH:MM[:SS]" 转成毫秒时间戳。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int((dt - timedelta(hours=8)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return None


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(values: np.ndarray) -> List[Optional[float]]:
    """numpy → JSON 友好的列表（NaN/Inf → None）。"""
    out: List[Optional[float]] = []
    for v in values:
        f = float(v)
        out.append(f if np.isfinite(f) else None)
    return out


# --------------------------------------------------------------------------- 行情
async def fetch_klines(symbol: str, interval: str, limit: int = FETCH_BARS) -> Dict[str, Any]:
    """从**当前账户所在市场**抓取永续 K 线。测试网账户 → 测试网 K 线。"""
    from config.market_endpoints import resolve_for_account

    ep = resolve_for_account()
    if aiohttp is None:
        raise RuntimeError("aiohttp 不可用，无法抓取行情")
    url = ep.klines_url(interval=interval, symbol=symbol, limit=int(limit))
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            rows = await resp.json()
    return {"rows": rows, "url": f"{ep.rest}/fapi/v1/klines", "market": ep.market,
            "market_label": ep.label, "source": ep.source}


def _arrays(rows: List[List[Any]]) -> Dict[str, np.ndarray]:
    arr = {
        "ts": np.array([int(r[0]) for r in rows], dtype=np.int64),
        "open": np.array([float(r[1]) for r in rows], dtype=np.float64),
        "high": np.array([float(r[2]) for r in rows], dtype=np.float64),
        "low": np.array([float(r[3]) for r in rows], dtype=np.float64),
        "close": np.array([float(r[4]) for r in rows], dtype=np.float64),
        "volume": np.array([float(r[5]) for r in rows], dtype=np.float64),
        "close_time": np.array([int(r[6]) for r in rows], dtype=np.int64),
    }
    return arr


def _signals_and_state(ts: np.ndarray, close: np.ndarray,
                       k: np.ndarray, d: np.ndarray,
                       hist: np.ndarray) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按部署规格逐根判定 B/S 信号，并推演策略虚拟方向（模式 A：对侧有效信号反手）。

    返回 (信号列表, 每根 K 线的推演方向 long/short/flat)。
    """
    n = len(close)
    out: List[Dict[str, Any]] = []
    state: List[str] = []
    pos = 0  # 0 空仓 / 1 多 / -1 空
    for i in range(n):
        if i == 0:
            gold = dead = False
        else:
            gold, dead = _SIG.crossing(float(k[i - 1]), float(d[i - 1]),
                                       float(k[i]), float(d[i]))
        sig_long, sig_short, note = _SIG.macd_gate(gold, dead, float(hist[i]), enabled=True)
        if sig_long:
            # 部署口径是**单仓、不加仓**（max_concurrent = 1）：
            # 已持多时再出同向信号不动作，只有对侧信号才反手。
            if pos == 1:
                action, kind = "重复信号（已持多，不动作）", "repeat"
            elif pos == -1:
                action, kind = "反手做多", "reverse"
            else:
                action, kind = "开多", "open"
            pos = 1
            out.append({"i": i, "t": int(ts[i]), "side": "B", "kind": kind, "action": action,
                        "reason": "金叉 + MACD 绿柱（柱为正）", "k": float(k[i]),
                        "d": float(d[i]), "hist": float(hist[i])})
        elif sig_short:
            if pos == -1:
                action, kind = "重复信号（已持空，不动作）", "repeat"
            elif pos == 1:
                action, kind = "反手做空", "reverse"
            else:
                action, kind = "开空", "open"
            pos = -1
            out.append({"i": i, "t": int(ts[i]), "side": "S", "kind": kind, "action": action,
                        "reason": "死叉 + MACD 红柱（柱为负）", "k": float(k[i]),
                        "d": float(d[i]), "hist": float(hist[i])})
        elif note:
            pass  # 方向背离 → 丢弃，不动仓（方向保持）
        state.append("long" if pos == 1 else ("short" if pos == -1 else "flat"))
    return out, state


# --------------------------------------------------------------------------- 本地成交
def read_local_trades(symbol: str, interval: str) -> List[Dict[str, Any]]:
    """读取运行器落盘的策略成交日志（非「观察」行），用于在图上一并核对。"""
    if not os.path.exists(TRADES_CSV):
        return []
    coin = "BTC" if symbol.upper().startswith("BTC") else "ETH"
    out: List[Dict[str, Any]] = []
    try:
        with open(TRADES_CSV, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                action = str(row.get("动作") or "")
                if coin not in action or interval not in action:
                    continue
                if "观察" in action:
                    continue
                ms = _beijing_to_ms(str(row.get("时间") or ""))
                if ms is None:
                    continue
                out.append({
                    "t": ms,
                    "action": action,
                    "side": str(row.get("方向") or ""),
                    "qty": _num(row.get("数量")),
                    "price": _num(row.get("价格")),
                    "net_pnl": _num(row.get("净盈亏")),
                    "k": _num(row.get("K")),
                    "d": _num(row.get("D")),
                    "equity": _num(row.get("权益")),
                    "note": str(row.get("说明") or ""),
                })
    except Exception:
        return []
    out.sort(key=lambda r: r["t"])
    return out


# --------------------------------------------------------------------------- 组装
def read_runner_reading(symbol: str, interval: str) -> Optional[Dict[str, Any]]:
    """读取运行器落盘的读数快照（bot 真正使用的那根 K 线）。"""
    name = READING_FILES.get((symbol, interval))
    if not name:
        return None
    path = os.path.join(ROOT, "runtime", "shadow", name)
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if str(d.get("symbol") or "").upper() != symbol:
        return None
    if str(d.get("interval") or "").lower() != interval:
        return None
    return {
        "bar_ms": int(d.get("bar_ms") or 0),
        "bar_utc": d.get("bar_utc"),
        "close": _num(d.get("close")),
        "k": _num(d.get("K")), "d": _num(d.get("D")),
        "dif": _num(d.get("MACD_DIF")), "dea": _num(d.get("MACD_DEA")),
        "hist": _num(d.get("MACD_HIST")),
        "position": d.get("position"),
        "signal_long": bool(d.get("signal_long")), "signal_short": bool(d.get("signal_short")),
        "gold": bool(d.get("gold")), "dead": bool(d.get("dead")),
        "missed_bars": d.get("missed_bars"),
        "market": d.get("market"),
        "kline_url": d.get("kline_url"),
        "updated_ms": int(d.get("updated_ms") or 0),
    }


def compare_with_runner(ts: np.ndarray, k: np.ndarray, d: np.ndarray,
                        dif: np.ndarray, dea: np.ndarray, hist: np.ndarray,
                        runner: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把本模块算出的指标与运行器读数在同一根 K 线上逐项比对。"""
    if not runner or not runner.get("bar_ms"):
        return None
    hits = np.nonzero(ts == runner["bar_ms"])[0]
    if not len(hits):
        return {"bar_ms": runner["bar_ms"], "found": False,
                "note": "该根 K 线不在本次抓取窗口内"}
    i = int(hits[0])
    fields = (("k", float(k[i])), ("d", float(d[i])), ("dif", float(dif[i])),
              ("dea", float(dea[i])), ("hist", float(hist[i])))
    diffs: Dict[str, Any] = {}
    ok = True
    for name, mine in fields:
        theirs = runner.get(name)
        delta = None if theirs is None else abs(mine - float(theirs))
        diffs[name] = {"chart": mine, "runner": theirs, "abs_diff": delta}
        if delta is not None and delta > 1e-6:
            ok = False
    return {"bar_ms": runner["bar_ms"], "found": True, "match": ok, "fields": diffs}


def build_payload(symbol: str, interval: str, display_bars: int,
                  rows: List[List[Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    """同步组装一份图表数据（由 async 入口在抓取完成后调用）。"""
    arr = _arrays(rows)
    ts, close = arr["ts"], arr["close"]

    k, d, j = _IND.kdj(arr["high"], arr["low"], close, *KDJ_PARAMS)
    dif, dea, hist = _IND.macd(close, *MACD_PARAMS)
    signals, state = _signals_and_state(ts, close, k, d, hist)

    start = max(0, len(close) - int(display_bars))
    bars = []
    for i in range(start, len(close)):
        bars.append({
            "t": int(ts[i]), "ct": int(arr["close_time"][i]),
            "o": float(arr["open"][i]), "h": float(arr["high"][i]),
            "l": float(arr["low"][i]), "c": float(close[i]),
            "v": float(arr["volume"][i]),
            "k": float(k[i]), "d": float(d[i]), "j": float(j[i]),
            "dif": float(dif[i]), "dea": float(dea[i]), "hist": float(hist[i]),
            "pos": state[i],
        })
    sigs = [s for s in signals if s["i"] >= start]

    latest = bars[-1] if bars else None
    prev_close = bars[-2]["c"] if len(bars) > 1 else None
    runner = read_runner_reading(symbol, interval)
    parity = compare_with_runner(ts, k, d, dif, dea, hist, runner)
    return {
        "ok": True,
        "module": "图片显示确认模块",
        "symbol": symbol,
        "interval": interval,
        "strategy_key": STRATEGY_KEYS.get((symbol, interval), ""),
        "market": meta["market"],
        "market_label": meta["market_label"],
        "kline_url": meta["url"],
        "endpoint_source": meta["source"],
        "server_time_ms": int(time.time() * 1000),
        "fetched_bars": int(len(close)),
        "displayed_bars": len(bars),
        "params": {"kdj": list(KDJ_PARAMS), "macd": list(MACD_PARAMS)},
        "signal_rule": SIGNAL_RULE,
        "bars": bars,
        "signals": sigs,
        "trades": read_local_trades(symbol, interval),
        "latest": latest,
        "prev_close": prev_close,
        # 运行器读数与逐项比对 —— 证明「图上的数值 = bot 真正用的数值」。
        "runner": runner,
        "parity": parity,
        "notes": [
            "K 线与指标来自测试网行情；KDJ/MACD 与运行器使用同一份函数。",
            "B = 金叉且 MACD 柱为正；S = 死叉且 MACD 柱为负；背离的交叉已丢弃。",
            "标记画在信号 K 线上，实际执行在其下一根 K 线开盘。",
            f"指标预热 {FETCH_BARS} 根，展示最近 {len(bars)} 根。",
        ],
    }


async def get_chart(symbol: str, interval: str,
                    display_bars: int = DEFAULT_DISPLAY_BARS,
                    use_cache: bool = True) -> Dict[str, Any]:
    """对外入口：抓取 + 计算 + 缓存。"""
    symbol = (symbol or "BTCUSDT").upper()
    interval = (interval or "15m").lower()
    if symbol not in SYMBOLS:
        raise ValueError(f"不支持的标的: {symbol}")
    if interval not in INTERVALS:
        raise ValueError(f"不支持的周期: {interval}")
    display_bars = max(50, min(MAX_DISPLAY_BARS, int(display_bars or DEFAULT_DISPLAY_BARS)))

    key = (symbol, interval, display_bars)
    now = time.time()
    if use_cache:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]

    fetched = await fetch_klines(symbol, interval, FETCH_BARS)
    payload = build_payload(symbol, interval, display_bars, fetched["rows"], fetched)
    _CACHE[key] = (now, payload)
    return payload


def self_test(symbol: str = "BTCUSDT", interval: str = "15m") -> str:
    """离线自检：不联网，用合成数据验证指标与信号链路。"""
    import math as _m
    n = 300
    ts = np.arange(n, dtype=np.int64) * 900_000 + 1_700_000_000_000
    close = np.array([100 + 10 * _m.sin(i / 12.0) for i in range(n)], dtype=np.float64)
    high = close + 0.6
    low = close - 0.6
    k, d, j = _IND.kdj(high, low, close, *KDJ_PARAMS)
    dif, dea, hist = _IND.macd(close, *MACD_PARAMS)
    sigs, state = _signals_and_state(ts, close, k, d, hist)
    return (f"KDJ 末值 K={k[-1]:.2f} D={d[-1]:.2f} | MACD 末值 DIF={dif[-1]:.4f} "
            f"DEA={dea[-1]:.4f} HIST={hist[-1]:.4f} | 信号数={len(sigs)} "
            f"| 末态={state[-1]}")


if __name__ == "__main__":  # pragma: no cover
    print(self_test())
