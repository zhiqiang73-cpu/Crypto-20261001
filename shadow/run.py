"""影子模式运行入口.

用法:
    python3 -m shadow.run --equity 1000 --out runtime/shadow
    python3 -m shadow.run --tail 30          # 只看最近 30 天的结果

默认对本地 15m / 1h 数据做一次完整回放 (等价于把影子模式跑过历史),
输出逐根日志、逐笔日志、日报、周报。
真实下单接口不参与任何环节。
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from shadow.engine import ShadowConfig, run_shadow
from shadow.reporting import (daily_reports, weekly_summary, write_bar_log,
                              write_skip_log, write_trade_log)

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA = os.path.join(ROOT, "inputs", "klines")


def load(path: str) -> dict:
    df = pd.read_csv(path).rename(columns={"open_time": "ts"})
    df = df.sort_values("ts").reset_index(drop=True)
    return {k: df[k].to_numpy() for k in ("ts", "open", "high", "low", "close", "volume")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1000.0, help="初始权益 USDT")
    ap.add_argument("--out", default=os.path.join(ROOT, "runtime", "shadow"))
    ap.add_argument("--tail", type=int, default=0, help="只用最近 N 天数据")
    args = ap.parse_args()

    b15 = load(os.path.join(DATA, "BTCUSDT_15m.csv"))
    b1h = load(os.path.join(DATA, "BTCUSDT_1h.csv"))

    if args.tail:
        cutoff = b15["ts"][-1] - args.tail * 86_400_000
        m = b15["ts"] >= cutoff
        b15 = {k: v[m] for k, v in b15.items()}
        # 1H 需覆盖同样的起点, 额外多留 30 天做 ATR 预热
        m1 = b1h["ts"] >= cutoff - 30 * 86_400_000
        b1h = {k: v[m1] for k, v in b1h.items()}

    print(f"15m K线: {len(b15['ts']):,} 根  "
          f"{datetime.fromtimestamp(b15['ts'][0]/1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
          f" → {datetime.fromtimestamp(b15['ts'][-1]/1000, tz=timezone.utc):%Y-%m-%d %H:%M}")
    print(f"1H  K线: {len(b1h['ts']):,} 根")
    print(f"初始权益: {args.equity:.2f} USDT\n")

    res = run_shadow(b15, b1h, ShadowConfig(equity0=args.equity))

    write_bar_log(res, os.path.join(args.out, "bar_log.csv"))
    write_trade_log(res, os.path.join(args.out, "trade_log.csv"))
    write_skip_log(res, os.path.join(args.out, "skipped_entries.csv"))
    days = daily_reports(res, os.path.join(args.out, "daily"), args.equity)
    wk = weekly_summary(res, os.path.join(args.out, "weekly"), args.equity)

    n_long = sum(1 for b in res.bars if b.sig_long)
    n_short = sum(1 for b in res.bars if b.sig_short)
    n_ll = sum(1 for b in res.bars if b.loose_long)
    n_ls = sum(1 for b in res.bars if b.loose_short)
    ta, tb = res.trades_a, res.trades_b

    def stat(ts_, name, eq, peak):
        if not ts_:
            print(f"  {name}: 无成交")
            return
        wins = [t for t in ts_ if t.net > 0]
        fee = sum(t.entry_fee + t.exit_fee for t in ts_)
        gross = sum(t.gross for t in ts_)
        print(f"  {name}: 成交 {len(ts_)} 笔 | 胜率 {len(wins)/len(ts_):.1%} | "
              f"毛盈亏 {gross:+.2f} | 净盈亏 {sum(t.net for t in ts_):+.2f} | "
              f"手续费 {fee:.2f} | 最终权益 {eq:.2f} ({(eq/args.equity-1):+.2%}) | "
              f"最大回撤 {(peak-eq)/peak if peak else 0:.1%}")
        if wins:
            aw = sum(t.net for t in wins) / len(wins)
            ls = [t for t in ts_ if t.net <= 0]
            al = sum(t.net for t in ls) / len(ls) if ls else 0.0
            print(f"        平均盈利 {aw:+.2f} | 平均亏损 {al:+.2f} | "
                  f"盈亏比 {aw/abs(al) if al else float('nan'):.2f} | "
                  f"平均持仓 {np.mean([t.hold_min for t in ts_]):.0f} 分钟")

    print("=" * 88)
    print("  信号统计 (15m, 只统计已收盘 K 线)")
    print("=" * 88)
    print(f"  严格做多 {n_long}  严格做空 {n_short}  合计 {n_long + n_short}")
    print(f"  宽松做多 {n_ll}  宽松做空 {n_ls}  合计 {n_ll + n_ls}")
    print(f"  因闸门/数量跳过 {len(res.skips)} 笔")
    print(f"  风控熔断 {len(res.halts)} 次")
    for hh in res.halts:
        print(f"    {datetime.fromtimestamp(hh['ts']/1000, tz=timezone.utc):%Y-%m-%d %H:%M} "
              f"{hh['reason']} 回撤={hh['drawdown']:.1%} 权益={hh['equity']:.2f}")
    print()
    print("=" * 88)
    print("  虚拟成交结果")
    print("=" * 88)
    stat(ta, "模式 A (对侧完整信号平仓反手)", res.final_equity_a, res.peak_a)
    stat(tb, "模式 B (对侧裸交叉平仓)     ", res.final_equity_b, res.peak_b)
    print()
    print(f"  输出目录: {args.out}")
    print(f"    bar_log.csv / trade_log.csv / skipped_entries.csv")
    print(f"    daily/ ({len(days)} 天)  weekly/{os.path.basename(wk)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
