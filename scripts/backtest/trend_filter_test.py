"""决定性验证: 「价格在长期均线上方就持有, 下方就空仓」到底成不成立.

这是前面所有扫描里唯一在 1d/4h 上同时打败买入持有、且回撤大幅更小的规则。

要排除"参数巧合", 必须同时满足:
  1. 均线周期从 100 到 300 一整片都有效 —— 而不是只有 200 这一个点好。
  2. 在 1d / 4h 上样本外仍然成立。
  3. 回撤按**逐根盯市**算 (之前那版只在平仓时更新净值, 严重低估回撤)。
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
KLINES = ROOT / "inputs" / "klines"
COST_PER_SIDE = 0.0005 + 0.0002
BARS_PER_YEAR = {"1d": 365.0, "4h": 365.0 * 6, "1h": 365.0 * 24}


@dataclass
class Bar:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


def load_bars(path: Path) -> List[Bar]:
    out: List[Bar] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out.append(Bar(int(r["open_time"]), float(r["open"]), float(r["high"]),
                               float(r["low"]), float(r["close"]), float(r["volume"])))
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda b: b.ts)
    return out


def sma(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


@dataclass
class Stats:
    ret: float = 0.0
    cagr: float = 0.0
    sharpe: float = 0.0
    mdd: float = 0.0
    exposure: float = 0.0
    flips: int = 0

    def row(self) -> str:
        return (f"{self.ret:>9.1%} {self.cagr:>7.1%} {self.sharpe:>7.2f} "
                f"{self.mdd:>8.1%} {self.exposure:>8.1%} {self.flips:>6d}")


def backtest(bars: Sequence[Bar], target: Sequence[Optional[int]],
             bpy: float, start: int, end: int) -> Stats:
    """逐根盯市, 下一根开盘成交."""
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    pos = 0
    entry_px = 0.0
    rets: List[float] = []
    flips = 0
    in_market = 0
    total = 0
    prev_close = bars[max(start, 1)].c

    for i in range(max(start, 1), end - 1):
        nb = bars[i + 1]
        want = target[i]
        if want is None:
            want = pos
        if want != pos:
            px = nb.o
            if pos != 0 and entry_px:
                equity *= (1.0 + (px / entry_px - 1.0) * pos - 2 * COST_PER_SIDE)
            pos = want
            entry_px = px if pos != 0 else 0.0
            flips += 1

        # 盯市: 已实现净值 × (1 + 未实现)
        unreal = (nb.c / entry_px - 1.0) * pos if (pos != 0 and entry_px) else 0.0
        mtm = equity * (1.0 + unreal)
        peak = max(peak, mtm)
        if peak > 0:
            mdd = max(mdd, 1.0 - mtm / peak)

        rets.append((nb.c / prev_close - 1.0) * pos)
        prev_close = nb.c
        total += 1
        if pos != 0:
            in_market += 1

    if pos != 0 and entry_px:
        equity *= (1.0 + (bars[end - 1].c / entry_px - 1.0) * pos - 2 * COST_PER_SIDE)

    sharpe = 0.0
    if len(rets) > 1:
        sd = statistics.stdev(rets)
        if sd > 0:
            sharpe = statistics.mean(rets) / sd * math.sqrt(bpy)
    years = max((bars[end - 1].ts - bars[start].ts) / 1000 / 86400 / 365, 1e-9)
    return Stats(
        ret=equity - 1.0,
        cagr=(equity ** (1 / years) - 1) if equity > 0 else -1.0,
        sharpe=sharpe, mdd=mdd,
        exposure=(in_market / total) if total else 0.0,
        flips=flips,
    )


def main() -> int:
    periods = (100, 150, 200, 250, 300)

    for tf in ("1d", "4h", "1h"):
        bars = load_bars(KLINES / f"BTCUSDT_{tf}.csv")
        bpy = BARS_PER_YEAR[tf]
        split = int(len(bars) * 0.70)
        closes = [b.c for b in bars]

        print(f"\n{'=' * 100}")
        print(f"  BTCUSDT {tf}  |  样本内 →{datetime.fromtimestamp(bars[split-1].ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f"   样本外 →{datetime.fromtimestamp(bars[-1].ts/1000, tz=timezone.utc):%Y-%m-%d}")
        print(f"{'=' * 100}")
        print(f"{'策略':<20} | {'全样本: 收益    CAGR   夏普    回撤   在场   换手':<46} | "
              f"{'样本外: 收益    CAGR   夏普    回撤   在场'}")
        print("-" * 100)

        bh = [1] * len(bars)
        f = backtest(bars, bh, bpy, 0, len(bars))
        o = backtest(bars, bh, bpy, split, len(bars))
        print(f"{'B&H 买入持有':<20} | {f.row():<46} | "
              f"{o.ret:>9.1%} {o.cagr:>7.1%} {o.sharpe:>7.2f} {o.mdd:>8.1%} {o.exposure:>8.1%}")

        for n in periods:
            m = sma(closes, n)
            sig: List[Optional[int]] = [0 if x is None else (1 if c > x else 0)
                                        for c, x in zip(closes, m)]
            f = backtest(bars, sig, bpy, 0, len(bars))
            o = backtest(bars, sig, bpy, split, len(bars))
            print(f"{f'SMA{n} 多头过滤':<20} | {f.row():<46} | "
                  f"{o.ret:>9.1%} {o.cagr:>7.1%} {o.sharpe:>7.2f} {o.mdd:>8.1%} {o.exposure:>8.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
