"""影子模式实时运行器 (阶段 1).

每 60 秒轮询一次币安公开行情, 只处理【已收盘】的 15m K 线,
按规格判定信号、做虚拟成交、追加日志。不触碰任何下单接口。

状态保存在 runtime/shadow/live_state.json, 中断后可续跑。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone

import numpy as np
import urllib.request

from shadow.engine import (ATR_MULT_K, BOLL_GATE_ENABLED, DISASTER_ATR, FEE_PER_SIDE, GATE_FEE_RATE,
                           GATE_STRONG, GATE_WEAK, MIN_NOTIONAL, MIN_QTY, RISK_R, STEP_SIZE,
                           floor_step)
from shadow.indicators import atr_wilder, boll, kdj
from shadow.signals import crossing
from shadow.reporting import BAR_COLS, TRADE_COLS
from config.market_endpoints import resolve_for_account

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "runtime", "shadow")
STATE = os.path.join(OUT, "live_state.json")
BAR_LOG = os.path.join(OUT, "live_bar_log.csv")
TRADE_LOG = os.path.join(OUT, "live_trade_log.csv")

# ---------------------------------------------------------------------------
# 行情地址 —— 必须跟着账户走, 不能硬编码。
#
# 2026-10-02 事故: 这里曾写死主网 K 线地址, 而下单走测试网,
# 导致 bot 用主网 K 线算 KDJ、在测试网下单, 信号错位 2 根 K 线(30 分钟)。
# 现在地址由 config.market_endpoints 依据账户 base_url 反推, 两者不可能分叉。
# ---------------------------------------------------------------------------
ENDPOINTS = resolve_for_account()
MARKET = ENDPOINTS.market
BASE = ENDPOINTS.rest + "/fapi/v1/klines"
UA = {"User-Agent": "crypto-quant-shadow/1.0"}


INTERVAL_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
}


def fetch(interval: str, limit: int, symbol: str = "BTCUSDT") -> dict:
    url = ENDPOINTS.klines_url(interval=interval, symbol=symbol, limit=limit)
    req = urllib.request.Request(url, headers=UA)
    rows = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    return {
        "ts": ts,
        "open": np.array([float(r[1]) for r in rows]),
        "high": np.array([float(r[2]) for r in rows]),
        "low": np.array([float(r[3]) for r in rows]),
        "close": np.array([float(r[4]) for r in rows]),
        "volume": np.array([float(r[5]) for r in rows]),
    }


def closed_kdj_series(interval: str, n: int = 36, symbol: str = "BTCUSDT") -> dict:
    """已收盘 K/D 尾段，给读数图用。行情腿与运行器同一套 fetch。"""
    now = int(time.time() * 1000)
    step = INTERVAL_MS.get(interval, INTERVAL_MS["15m"])
    bars = fetch(interval, max(80, n + 24), symbol)
    mask = (bars["ts"] + step) <= now
    high, low, close = bars["high"][mask], bars["low"][mask], bars["close"][mask]
    k, d, _j = kdj(high, low, close)
    k, d = k[-n:], d[-n:]

    def py(arr):
        out = []
        for value in arr:
            number = float(value)
            out.append(number if np.isfinite(number) else None)
        return out

    gold = dead = False
    if len(k) >= 2 and all(np.isfinite(float(x)) for x in (k[-2], d[-2], k[-1], d[-1])):
        gold, dead = crossing(float(k[-2]), float(d[-2]), float(k[-1]), float(d[-1]))
    return {"k": py(k), "d": py(d), "gold": bool(gold), "dead": bool(dead)}


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_state(equity0: float) -> dict:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"equity": equity0, "peak": equity0, "day": None, "day_start_eq": equity0,
            "halted": False, "pos": None, "last_ts": 0,
            "equity_b": equity0, "peak_b": equity0, "day_b": None,
            "day_start_eq_b": equity0, "pos_b": None, "trades_a": 0, "trades_b": 0}


def save_state(st: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


def append_csv(path: str, header: list, row: list) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


def cycle(st: dict) -> int:
    """处理所有尚未处理的已收盘 15m K 线。返回处理根数。"""
    b15 = fetch("15m", 500)
    b1h = fetch("1h", 300)

    # 只保留已收盘的 15m (open_time + 15min <= now)
    now_ms = int(time.time() * 1000)
    closed = (b15["ts"] + 15 * 60 * 1000) <= now_ms
    b15 = {k: v[closed] for k, v in b15.items()}
    closed1 = (b1h["ts"] + 60 * 60 * 1000) <= now_ms
    b1h = {k: v[closed1] for k, v in b1h.items()}

    ts = b15["ts"]
    o, h, l, c, v = (b15[x] for x in ("open", "high", "low", "close", "volume"))
    k, d, j = kdj(h, l, c)
    mb, up, lb, _ = boll(c, 20, 2.0)

    atr1h = atr_wilder(b1h["high"], b1h["low"], b1h["close"], 14)
    close15 = ts + 15 * 60 * 1000
    close1h = b1h["ts"] + 60 * 60 * 1000
    idx = np.searchsorted(close1h, close15, side="right") - 1
    atr_al = np.full(len(ts), np.nan)
    ok = idx >= 0
    atr_al[ok] = atr1h[idx[ok]]

    processed = 0
    for i in range(1, len(ts)):
        if ts[i] <= st["last_ts"]:
            continue
        if np.isnan(up[i]) or np.isnan(atr_al[i]):
            st["last_ts"] = int(ts[i])
            continue

        day = int(ts[i]) // 86_400_000
        px_next = float(o[i])          # 若 i 是最后一根已收盘, 则用它自己的开盘代表"下一根开盘"占位
        ms = int(ts[i])

        # --- 盯市 + 风控 (模式 A) ---
        pos = st["pos"]
        mtm = st["equity"] + ((float(c[i]) - pos["px"]) * pos["qty"] * pos["side"]
                              if pos else 0.0)
        st["peak"] = max(st["peak"], mtm)
        if st["day"] != day:
            st["day"], st["day_start_eq"] = day, mtm
        dl = (st["day_start_eq"] - mtm) / st["day_start_eq"] if st["day_start_eq"] else 0.0
        dd = (st["peak"] - mtm) / st["peak"] if st["peak"] else 0.0
        if dd >= 0.10 and not st["halted"]:
            st["halted"] = True
            print(f"[熔断] {_fmt(ms)} 累计回撤 {dd:.1%} ≥ 10% —— 全部停止, 等待人工指令")
        block = st["halted"] or dl >= 0.03

        # --- 灾难止损 ---
        if pos:
            sd = DISASTER_ATR * pos["atr"]
            trig = None
            if pos["side"] == 1 and float(l[i]) <= pos["px"] - sd:
                trig = pos["px"] - sd
            elif pos["side"] == -1 and float(h[i]) >= pos["px"] + sd:
                trig = pos["px"] + sd
            if trig is not None:
                gross = (trig - pos["px"]) * pos["qty"] * pos["side"]
                fee = trig * pos["qty"] * FEE_PER_SIDE
                st["equity"] += gross - pos["fee"] - fee
                append_csv(TRADE_LOG, TRADE_COLS, [
                    "A", pos["side"], _fmt(pos["ms"]), f"{pos['px']:.2f}",
                    f"{pos['qty']:.4f}", f"{pos['fee']:.4f}", _fmt(ms), f"{trig:.2f}",
                    f"{fee:.4f}", f"{(ms - pos['ms'])/60000:.1f}", f"{gross:.4f}",
                    f"{gross - pos['fee'] - fee:.4f}", f"{pos['k']:.4f}", f"{pos['d']:.4f}",
                    f"{pos['atr']:.4f}", f"{pos['bw']:.4f}", f"{pos['mult']:.4f}",
                    f"{pos['fp']:.4f}", "灾难止损"])
                st["trades_a"] += 1
                st["pos"] = None
                pos = None

        # --- 信号 ---
        gold, dead = crossing(k[i - 1], d[i - 1], k[i], d[i])
        sig_long, sig_short = gold, dead
        loose_long, loose_short = gold, dead  # 兼容旧日志列名

        bw = float(up[i] - lb[i])
        target = bw / 2.0
        fp = GATE_FEE_RATE * float(c[i])
        mult = target / fp if fp > 0 else 0.0
        r_eff = (RISK_R if not BOLL_GATE_ENABLED else
                 (RISK_R if mult >= GATE_STRONG else
                  (RISK_R / 2.0 if mult >= GATE_WEAK else None)))

        # --- 执行: 用"下一根已收盘 K 线"的开盘; 若尚无下一根则留待下轮 ---
        nxt = i + 1
        if nxt >= len(ts):
            break                      # 等下一根收盘再成交
        px_fill = float(o[nxt])
        ms_fill = int(ts[nxt])

        if st["pos"] is None:
            want = 1 if sig_long else (-1 if sig_short else 0)
            if want and not block and r_eff is not None:
                qty = floor_step(mtm * r_eff / (ATR_MULT_K * float(atr_al[i])))
                if qty >= MIN_QTY and qty * px_fill >= MIN_NOTIONAL:
                    fee = px_fill * qty * FEE_PER_SIDE
                    st["equity"] -= fee
                    st["pos"] = {"side": want, "ms": ms_fill, "px": px_fill, "qty": qty,
                                 "fee": fee, "k": float(k[i]), "d": float(d[i]),
                                 "atr": float(atr_al[i]), "bw": bw, "mult": mult, "fp": fp}
                    print(f"[开仓] {_fmt(ms_fill)} {'多' if want==1 else '空'} "
                          f"{qty:.4f} @ {px_fill:.2f} (K={k[i]:.1f} 倍数={mult:.2f})")
        else:
            pos = st["pos"]
            opp = (pos["side"] == 1 and sig_short) or (pos["side"] == -1 and sig_long)
            if opp:
                gross = (px_fill - pos["px"]) * pos["qty"] * pos["side"]
                fee = px_fill * pos["qty"] * FEE_PER_SIDE
                st["equity"] += gross - pos["fee"] - fee
                append_csv(TRADE_LOG, TRADE_COLS, [
                    "A", pos["side"], _fmt(pos["ms"]), f"{pos['px']:.2f}",
                    f"{pos['qty']:.4f}", f"{pos['fee']:.4f}", _fmt(ms_fill),
                    f"{px_fill:.2f}", f"{fee:.4f}",
                    f"{(ms_fill - pos['ms'])/60000:.1f}", f"{gross:.4f}",
                    f"{gross - pos['fee'] - fee:.4f}", f"{pos['k']:.4f}", f"{pos['d']:.4f}",
                    f"{pos['atr']:.4f}", f"{pos['bw']:.4f}", f"{pos['mult']:.4f}",
                    f"{pos['fp']:.4f}", "信号反转"])
                st["trades_a"] += 1
                print(f"[平仓] {_fmt(ms_fill)} {'多' if pos['side']==1 else '空'} "
                      f"净 {gross - pos['fee'] - fee:+.2f} USDT")
                st["pos"] = None

        append_csv(BAR_LOG, BAR_COLS, [
            _fmt(ms), f"{o[i]:.2f}", f"{h[i]:.2f}", f"{l[i]:.2f}", f"{c[i]:.2f}",
            f"{v[i]:.4f}", f"{k[i]:.4f}", f"{d[i]:.4f}", f"{j[i]:.4f}",
            f"{atr_al[i]:.4f}", f"{up[i]:.4f}", f"{mb[i]:.4f}", f"{lb[i]:.4f}",
            f"{bw:.4f}", f"{target:.4f}", f"{mult:.4f}",
            "Y" if gold else "", "Y" if dead else "", "Y" if sig_long else "",
            "Y" if sig_short else "", "Y" if loose_long else "",
            "Y" if loose_short else "",
            st["pos"]["side"] if st["pos"] else 0,
            f"{st['pos']['qty']:.4f}" if st["pos"] else "0.0000"])
        st["last_ts"] = int(ts[i])
        processed += 1

    save_state(st)
    return processed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--interval", type=int, default=60, help="轮询秒数")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    st = load_state(args.equity)
    print(f"影子模式启动 | 权益 {st['equity']:.2f} USDT | 已处理至 "
          f"{_fmt(st['last_ts']) if st['last_ts'] else '(首次)'} | 熔断={st['halted']}")
    while True:
        try:
            n = cycle(st)
            if n:
                print(f"[{datetime.now():%H:%M:%S}] 处理 {n} 根新 K 线 | 权益 "
                      f"{st['equity']:.2f} | 持仓 "
                      f"{st['pos']['side'] if st['pos'] else '空仓'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{datetime.now():%H:%M:%S}] 轮询异常: {exc}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
