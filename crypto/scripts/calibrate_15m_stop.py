"""15m 影子规格复跑对照：正常止损 × ATR_1H（+ 1.5×ATR 保本）在最新数据上的表现。

背景（2026-10-03 定案）：15m 影子侧 = 1.5×ATR_1H 止损 + 浮盈 1.5×ATR 后下根起保本
（shadow/engine.py 的 SPEC_15M 已带这两个参数，见 [[strategy-decisions-2026-10]]）。
本脚本在刷新后的行情上复跑指定窗口并扫描止损距离，验证该决定在新窗口
（例如 10-03 暴跌段进入样本后）是否仍然成立。

用法：
    python3 -m scripts.calibrate_15m_stop --days 60
    python3 -m scripts.calibrate_15m_stop --days 60 --stops 1.0,1.5,2.0 --mode B

只读本地 inputs/klines/*.csv，不联网、不下单。

口径提醒（同 5m 版）：收益率/回撤是复利结果，长历史里会被「仓位归零」压扁，
标定止损距离以 **R 倍数**（价格位移 ÷ 入场 ATR_1H）为准。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from shadow.engine import SPEC_15M, ShadowConfig, run_shadow_spec  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA = os.path.join(ROOT, "inputs", "klines")
DEFAULT_STOPS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
WARMUP_DAYS = 60          # 指标预热：多留一段数据，避免开头信号被截断
CHOSEN = 1.5              # 2026-10-03 定案的止损距离（×ATR_1H）


def load(path: str) -> dict:
    df = pd.read_csv(path).rename(columns={"open_time": "ts"})
    df = df.sort_values("ts").reset_index(drop=True)
    return {k: df[k].to_numpy() for k in ("ts", "open", "high", "low", "close", "volume")}


def metrics(res, equity0: float, mode: str) -> dict:
    trades = res.trades_a if mode == "A" else res.trades_b
    eq = res.final_equity_a if mode == "A" else res.final_equity_b
    peak = res.peak_a if mode == "A" else res.peak_b
    n = len(trades)
    if n == 0:
        return {"n": 0, "eq": eq, "ret": eq / equity0 - 1, "mdd": 0.0, "wr": 0.0,
                "pf": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "hold": 0.0,
                "fee": 0.0, "gross": 0.0, "net": 0.0, "R": float("nan"),
                "R_win": float("nan"), "R_loss": float("nan"),
                "stop": 0, "disaster": 0, "signal": 0, "halt_ts": None}
    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    aw = sum(t.net for t in wins) / len(wins) if wins else 0.0
    al = sum(t.net for t in losses) / len(losses) if losses else 0.0
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    rs = [(t.exit_px - t.entry_px) * t.side / t.atr_at_entry
          for t in trades if t.atr_at_entry]
    r_win = [r for r, t in zip(rs, [x for x in trades if x.atr_at_entry]) if t.net > 0]
    r_loss = [r for r, t in zip(rs, [x for x in trades if x.atr_at_entry]) if t.net <= 0]
    return {
        "n": n, "eq": eq, "ret": eq / equity0 - 1,
        "mdd": (peak - eq) / peak if peak else 0.0,
        "wr": len(wins) / n,
        "pf": (aw / abs(al)) if al else float("inf"),
        "avg_win": aw, "avg_loss": al,
        "hold": float(np.mean([t.hold_min for t in trades])),
        "fee": sum(t.entry_fee + t.exit_fee for t in trades),
        "gross": sum(t.gross for t in trades),
        "net": sum(t.net for t in trades),
        "R": float(np.mean(rs)) if rs else float("nan"),
        "R_win": float(np.mean(r_win)) if r_win else float("nan"),
        "R_loss": float(np.mean(r_loss)) if r_loss else float("nan"),
        "stop": reasons.get("止损", 0),
        "disaster": reasons.get("灾难止损", 0),
        "signal": sum(v for k, v in reasons.items() if k not in ("止损", "灾难止损")),
        "halt_ts": res.halts[0]["ts"] if res.halts else None,
    }


def scan(b15: dict, b1h: dict, stops, equity: float, mode: str,
         no_halt: bool) -> list:
    """对一组止损距离跑一遍回放；各行保持保本触发 = 1.5×ATR，末行为都关的参照。"""
    rows = []
    for k in list(stops):
        spec = replace(SPEC_15M, stop_atr_mult=k, halt_on_drawdown=not no_halt)
        res = run_shadow_spec(b15, b1h, spec, ShadowConfig(equity0=equity))
        m = metrics(res, equity, mode)
        m["stop_mult"] = k
        m["halts"] = len(res.halts)
        rows.append(m)
    spec_off = replace(SPEC_15M, stop_atr_mult=None,
                       break_even_trigger_atr_mult=None,
                       halt_on_drawdown=not no_halt)
    res = run_shadow_spec(b15, b1h, spec_off, ShadowConfig(equity0=equity))
    m = metrics(res, equity, mode)
    m["stop_mult"] = None
    m["halts"] = len(res.halts)
    rows.append(m)
    return rows


def _label(k) -> str:
    if k is None:
        return "都关(历史)"
    return f"{k:.2f}×ATR+保本" + ("*" if k == CHOSEN else "")


def _row(m: dict) -> str:
    return ("%-14s %7d %6.1f%% %+9.2f %8.2f%% %8.2f%% %8.2f %+8.3f %+8.3f %7d %7d %7d" % (
        _label(m["stop_mult"]), m["n"], m["wr"] * 100, m["net"], m["ret"] * 100,
        m["mdd"] * 100, m["pf"], m["R"], m["avg_loss"],
        m["stop"], m["disaster"], m["halts"]))


HDR = ("%-14s %7s %7s %9s %9s %9s %8s %8s %8s %7s %7s %7s" %
       ("规格", "笔数", "胜率", "净盈亏", "收益率", "最大回撤", "盈亏比",
        "平均R", "平均亏", "止损数", "灾难数", "熔断"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--days", type=int, default=0, help="只用最近 N 天；0 = 全部")
    ap.add_argument("--stops", default="", help="逗号分隔的止损倍数；留空用默认列表")
    ap.add_argument("--mode", default="A", choices=["A", "B"])
    ap.add_argument("--no-halt", action="store_true",
                    help="不因累计回撤锁死，用于看清策略本身的长期表现")
    args = ap.parse_args()

    sym = args.symbol.upper()
    f15 = os.path.join(DATA, f"{sym}_15m.csv")
    f1 = os.path.join(DATA, f"{sym}_1h.csv")
    for p in (f15, f1):
        if not os.path.exists(p):
            print(f"缺少数据文件：{p}\n请先运行 "
                  f"python3 -m scripts.fetch_klines --symbol {sym} --interval 15m",
                  file=sys.stderr)
            return 2

    b15 = load(f15)
    b1h = load(f1)
    if args.days:
        cutoff = int(b15["ts"][-1]) - args.days * 86_400_000
        m = b15["ts"] >= cutoff
        b15 = {k: v[m] for k, v in b15.items()}
        m1 = b1h["ts"] >= cutoff - WARMUP_DAYS * 86_400_000
        b1h = {k: v[m1] for k, v in b1h.items()}

    t0 = datetime.fromtimestamp(int(b15["ts"][0]) / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(int(b15["ts"][-1]) / 1000, tz=timezone.utc)
    print(f"标的 {sym}  周期 15m")
    print(f"15m K线 {len(b15['ts']):,} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}")
    print(f"1H K线 {len(b1h['ts']):,} 根（ATR 预热多留 {WARMUP_DAYS} 天）")
    print(f"初始权益 {args.equity:.2f} USDT   记录模式 {args.mode}"
          f"{'   熔断已关闭' if args.no_halt else ''}\n")

    stops = ([float(x) for x in args.stops.split(",") if x.strip()]
             if args.stops else DEFAULT_STOPS)

    rows = scan(b15, b1h, stops, args.equity, args.mode, args.no_halt)
    base = rows[-1]

    print("=" * 124)
    print(f"  15m 止损距离对照（模式 {args.mode}，含手续费；平均R = 价格位移 ÷ 入场ATR；* = 定案组合）")
    print("=" * 124)
    print(HDR)
    print("-" * 124)
    for m in rows:
        print(_row(m))
    print("-" * 124)

    chosen = next(m for m in rows if m["stop_mult"] == CHOSEN)
    print(f"  决策组合 {CHOSEN:.2f}×ATR+保本 vs 都关：{chosen['n']} vs {base['n']} 笔，"
          f"净盈亏 {chosen['net']:+.2f} vs {base['net']:+.2f}，"
          f"收益率 {chosen['ret']:+.2%} vs {base['ret']:+.2%}，"
          f"平均R {chosen['R']:+.3f} vs {base['R']:+.3f}，"
          f"最大回撤 {chosen['mdd']:.1%} vs {base['mdd']:.1%}")
    if chosen["halt_ts"]:
        ht = datetime.fromtimestamp(chosen["halt_ts"] / 1000, tz=timezone.utc)
        print(f"  决策组合首次熔断：{ht:%Y-%m-%d %H:%M}（共 {chosen['halts']} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
