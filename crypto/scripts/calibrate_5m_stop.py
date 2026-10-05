"""用实测数据标定 5m 的止损距离（正常止损 × ATR_1H）。

背景：5m 的出场原本只有「对侧交叉 + K 极值」，趋势中 K 回不到极值 → 没有出口，
只能扛到 3×ATR 灾难止损。本脚本给 5m 回放加一条正常止损，并扫出最合适的距离。

用法：
    python3 -m scripts.calibrate_5m_stop --symbol BTCUSDT
    python3 -m scripts.calibrate_5m_stop --symbol ETHUSDT --days 365
    python3 -m scripts.calibrate_5m_stop --symbol BTCUSDT --stops 1.0,1.25,1.5,2.0
    python3 -m scripts.calibrate_5m_stop --symbol BTCUSDT --by-year   # 逐年独立标定

只读本地 inputs/klines/*.csv，不联网、不下单。

两个口径上的要点（2026-10-03 补）：
  * 收益率/回撤是**复利**结果：权益缩到最小名义额以下后就不再交易，长历史的
    复利数字会被「仓位归零」压扁，不适合用来比较止损距离。
  * 因此本脚本同时给出 **R 倍数**：单笔「价格位移 ÷ 入场时的 ATR_1H」，
    完全与仓位、权益、复利无关，是标定止损距离的正确口径。
  * `--by-year` 让每一年独立回放（各自初始权益 1000），避免一次早期熔断把
    之后 5 年的样本全部锁死。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from shadow.engine import SPEC_5M, ShadowConfig, run_shadow_spec  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA = os.path.join(ROOT, "inputs", "klines")
DEFAULT_STOPS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
WARMUP_DAYS = 60          # 指标预热：多留一段数据，避免开头信号被截断
HOUR_MS = 60 * 60 * 1000


def load(path: str) -> dict:
    df = pd.read_csv(path).rename(columns={"open_time": "ts"})
    df = df.sort_values("ts").reset_index(drop=True)
    return {k: df[k].to_numpy() for k in ("ts", "open", "high", "low", "close", "volume")}


def slice_bars(bars: dict, lo_ms: int, hi_ms: int) -> dict:
    m = (bars["ts"] >= lo_ms) & (bars["ts"] < hi_ms)
    return {k: v[m] for k, v in bars.items()}


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
                "stop": 0, "disaster": 0, "signal": 0}
    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    aw = sum(t.net for t in wins) / len(wins) if wins else 0.0
    al = sum(t.net for t in losses) / len(losses) if losses else 0.0
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    # R 倍数：价格位移 ÷ 入场时的 ATR_1H，与仓位/权益/复利完全无关
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
    }


def scan(b5: dict, b1h: dict, stops, equity: float, mode: str,
         no_halt: bool) -> list:
    """对一组止损距离跑一遍回放，返回每档的指标（含不设止损的参照）。"""
    rows = []
    for k in list(stops) + [None]:
        spec = replace(SPEC_5M, stop_atr_mult=k, halt_on_drawdown=not no_halt)
        res = run_shadow_spec(b5, b1h, spec, ShadowConfig(equity0=equity))
        m = metrics(res, equity, mode)
        m["stop_mult"] = k
        m["halts"] = len(res.halts)
        rows.append(m)
    return rows


def _row(m: dict) -> str:
    label = "不设(历史)" if m["stop_mult"] is None else f"{m['stop_mult']:.2f}×ATR"
    return ("%-10s %7d %6.1f%% %+9.2f %8.2f%% %8.2f%% %8.2f %+8.3f %+8.3f %7d %7d %7d" % (
        label, m["n"], m["wr"] * 100, m["net"], m["ret"] * 100, m["mdd"] * 100,
        m["pf"], m["R"], m["avg_loss"], m["stop"], m["disaster"], m["halts"]))


HDR = ("%-10s %7s %7s %9s %9s %9s %8s %8s %8s %7s %7s %7s" %
       ("止损", "笔数", "胜率", "净盈亏", "收益率", "最大回撤", "盈亏比",
        "平均R", "平均亏", "止损数", "灾难数", "熔断"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--days", type=int, default=0, help="只用最近 N 天；0 = 全部")
    ap.add_argument("--stops", default="", help="逗号分隔的止损倍数；留空用默认扫描")
    ap.add_argument("--mode", default="A", choices=["A", "B"])
    ap.add_argument("--no-halt", action="store_true",
                    help="不因累计回撤锁死，用于看清策略本身的长期表现")
    ap.add_argument("--by-year", action="store_true",
                    help="逐年独立回放（各自初始权益），避免一次熔断锁死全部样本")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    sym = args.symbol.upper()
    f5 = os.path.join(DATA, f"{sym}_5m.csv")
    f1 = os.path.join(DATA, f"{sym}_1h.csv")
    for p in (f5, f1):
        if not os.path.exists(p):
            print(f"缺少数据文件：{p}\n请先运行 "
                  f"python3 -m scripts.fetch_klines --symbol {sym} --interval 5m", file=sys.stderr)
            return 2

    b5 = load(f5)
    b1h = load(f1)
    if args.days:
        cutoff = int(b5["ts"][-1]) - args.days * 86_400_000
        m = b5["ts"] >= cutoff
        b5 = {k: v[m] for k, v in b5.items()}
        m1 = b1h["ts"] >= cutoff - WARMUP_DAYS * 86_400_000
        b1h = {k: v[m1] for k, v in b1h.items()}

    t0 = datetime.fromtimestamp(int(b5["ts"][0]) / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(int(b5["ts"][-1]) / 1000, tz=timezone.utc)
    print(f"标的 {sym}  周期 5m")
    print(f"5m K线 {len(b5['ts']):,} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}")
    print(f"1H K线 {len(b1h['ts']):,} 根（ATR 预热多留 {WARMUP_DAYS} 天）")
    print(f"初始权益 {args.equity:.2f} USDT   记录模式 {args.mode}\n")

    stops = ([float(x) for x in args.stops.split(",") if x.strip()]
             if args.stops else DEFAULT_STOPS)

    # ---------- 全区间扫描 ----------
    rows = scan(b5, b1h, stops, args.equity, args.mode, args.no_halt)
    base = rows[-1]

    spec = replace(SPEC_5M, stop_atr_mult=None, halt_on_drawdown=not args.no_halt)
    res = run_shadow_spec(b5, b1h, spec, ShadowConfig(equity0=args.equity))
    n_long = sum(1 for b in res.bars if b.sig_long)
    n_short = sum(1 for b in res.bars if b.sig_short)
    n_ll = sum(1 for b in res.bars if b.loose_long)
    n_ls = sum(1 for b in res.bars if b.loose_short)
    print(f"  信号：严格 {n_long + n_short:,}（多 {n_long:,} / 空 {n_short:,}）"
          f"  裸交叉 {n_ll + n_ls:,}  跳过 {len(res.skips):,}  熔断 {len(res.halts)} 次")
    if res.halts:
        ht = datetime.fromtimestamp(res.halts[0]["ts"] / 1000, tz=timezone.utc)
        print(f"  首次熔断：{ht:%Y-%m-%d %H:%M}")
    print()

    print("=" * 124)
    print(f"  5m 止损距离扫描（模式 {args.mode}，含手续费；平均R = 价格位移 ÷ 入场ATR）")
    print("=" * 124)
    print(HDR)
    print("-" * 124)
    for m in rows[:-1]:
        print(_row(m))
    print("-" * 124)
    print(_row(base))
    print("-" * 124)

    usable = [m for m in rows[:-1] if m["n"] >= 20]
    if usable:
        best_r = max(usable, key=lambda m: m["R"])
        print(f"  → 样本 ≥20 笔中，期望 R 最高：{best_r['stop_mult']:.2f}×ATR"
              f"（平均R {best_r['R']:+.3f}，收益 {best_r['ret']:+.2%}，"
              f"盈亏比 {best_r['pf']:.2f}，被止损 {best_r['stop']} 笔）")

    # ---------- 逐年独立标定 ----------
    year_rows = []
    if args.by_year:
        print()
        print("=" * 124)
        print("  逐年独立标定（每年单独回放、各自初始权益；平均R / 收益率）")
        print("=" * 124)
        y0 = t0.year
        y1 = t1.year
        header = "%-6s" % "年份" + "".join("%18s" % (
            "无止损" if k is None else f"{k:.2f}×ATR") for k in list(stops) + [None])
        print(header)
        print("-" * 124)
        for year in range(y0, y1 + 1):
            lo = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            hi = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            s5 = slice_bars(b5, lo, hi)
            if len(s5["ts"]) < 500:
                continue
            s1 = slice_bars(b1h, lo - WARMUP_DAYS * 86_400_000, hi)
            yrows = scan(s5, s1, stops, args.equity, args.mode, args.no_halt)
            year_rows.append((year, yrows))
            cells = []
            for m in yrows:
                if m["n"] == 0:
                    cells.append("%18s" % "—")
                else:
                    cells.append("%18s" % f"{m['R']:+.3f} / {m['ret']:+.1%}")
            print("%-6d" % year + "".join(cells))
        print("-" * 124)
        # 跨年平均（只看有样本的年份）
        avg_cells = []
        for idx in range(len(stops) + 1):
            rs = [r[idx]["R"] for _, r in year_rows if r[idx]["n"] >= 5]
            avg_cells.append("%18s" % (f"{np.mean(rs):+.3f}" if rs else "—"))
        print("%-6s" % "均值" + "".join(avg_cells))
        print("-" * 124)

    out_dir = args.out or os.path.join(ROOT, "outputs", "5m_stop_calibration")
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, f"{sym}_5m_stop_scan.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["止损倍数", "笔数", "胜率", "净盈亏", "收益率", "最大回撤", "盈亏比",
                    "平均R", "平均盈利", "平均亏损", "平均持仓分钟", "手续费", "毛盈亏",
                    "止损笔数", "灾难止损笔数", "信号平仓笔数", "熔断次数"])
        for m in rows:
            w.writerow([m["stop_mult"] if m["stop_mult"] is not None else "",
                        m["n"], f"{m['wr']:.4f}", f"{m['net']:.4f}", f"{m['ret']:.4f}",
                        f"{m['mdd']:.4f}", f"{m['pf']:.4f}", f"{m['R']:.4f}",
                        f"{m['avg_win']:.4f}", f"{m['avg_loss']:.4f}", f"{m['hold']:.1f}",
                        f"{m['fee']:.4f}", f"{m['gross']:.4f}", m["stop"], m["disaster"],
                        m["signal"], m["halts"]])
    print(f"\n  结果已写入 {out_csv}")

    if year_rows:
        ycsv = os.path.join(out_dir, f"{sym}_5m_stop_by_year.csv")
        with open(ycsv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["年份", "止损倍数", "笔数", "胜率", "净盈亏", "收益率",
                        "最大回撤", "盈亏比", "平均R", "止损笔数", "灾难止损笔数"])
            for year, yrows in year_rows:
                for m in yrows:
                    w.writerow([year, m["stop_mult"] if m["stop_mult"] is not None else "",
                                m["n"], f"{m['wr']:.4f}", f"{m['net']:.4f}",
                                f"{m['ret']:.4f}", f"{m['mdd']:.4f}", f"{m['pf']:.4f}",
                                f"{m['R']:.4f}", m["stop"], m["disaster"]])
        print(f"  逐年结果已写入 {ycsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
