"""决定性检验: 只测「几乎没有参数可调」的策略.

为什么这一步最关键:
  前面两轮扫描里, 表现最好的组合在走步验证下全部崩掉。但那些组合都有
  4~5 个自由度 (窗口/倍数/持有期/过滤/止损), 本来就有足够空间去拟合噪声。

  真正诚实的检验是: 拿那些**教科书里早就写死、参数不来自本数据**的规则,
  看它们在 9 年数据上到底成不成立。如果连这些都过不了, 那就不是"参数没调好",
  而是这个市场在扣费后确实没有简单的免费午餐。

对照项:
  * B&H                        —— 必须打败的基准
  * 价格 vs SMA200 多头过滤      —— 最经典的趋势过滤器 (1 个参数, 且是惯例值)
  * 价格 vs SMA200 多空
  * 12 期动量 多头 / 多空         —— 时序动量, 学界记录最稳的异象之一
  * 唐奇安 55/20                —— 海龟交易法的原始参数, 非本数据拟合

每个都报: 全样本 / 样本内 / 样本外 的收益、夏普、最大回撤、笔数。
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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


# --------------------------------------------------------------------------- 回测
@dataclass
class Stats:
    ret: float = 0.0
    sharpe: float = 0.0
    mdd: float = 0.0
    trades: int = 0
    win: float = 0.0

    def row(self) -> str:
        return (f"{self.ret:>9.1%} {self.sharpe:>7.2f} {self.mdd:>8.1%} "
                f"{self.trades:>7d} {self.win:>7.1%}")


def backtest(bars: Sequence[Bar], target: Sequence[Optional[int]],
             bpy: float, start: int = 0, end: Optional[int] = None) -> Stats:
    end = len(bars) if end is None else end
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    pos = 0
    entry_px = 0.0
    rets: List[float] = []
    pnls: List[float] = []
    prev_close = None

    for i in range(max(start, 1), end - 1):
        b, nb = bars[i], bars[i + 1]
        want = target[i]
        if want is None:
            want = pos
        if want != pos:
            px = nb.o
            if pos != 0 and entry_px:
                net = (px / entry_px - 1.0) * pos - 2 * COST_PER_SIDE
                equity *= (1.0 + net)
                pnls.append(net)
            pos = want
            entry_px = px if pos != 0 else 0.0
        # 逐根盯市收益
        if prev_close is not None and pos != 0:
            rets.append((nb.c / prev_close - 1.0) * pos)
        prev_close = nb.c
        peak = max(peak, equity)
        mdd = max(mdd, 1.0 - equity / peak)

    if pos != 0 and entry_px:
        net = (bars[end - 1].c / entry_px - 1.0) * pos - 2 * COST_PER_SIDE
        equity *= (1.0 + net)
        pnls.append(net)

    sharpe = 0.0
    if len(rets) > 1:
        sd = statistics.stdev(rets)
        if sd > 0:
            sharpe = statistics.mean(rets) / sd * math.sqrt(bpy)
    wins = [p for p in pnls if p > 0]
    return Stats(ret=equity - 1.0, sharpe=sharpe, mdd=mdd, trades=len(pnls),
                 win=(len(wins) / len(pnls)) if pnls else 0.0)


# --------------------------------------------------------------------------- 策略
def s_buy_hold(bars: Sequence[Bar]) -> List[Optional[int]]:
    return [1] * len(bars)


def s_sma_filter(bars: Sequence[Bar], n: int, allow_short: bool) -> List[Optional[int]]:
    m = sma([b.c for b in bars], n)
    out: List[Optional[int]] = []
    for i, b in enumerate(bars):
        if m[i] is None:
            out.append(0)
        elif b.c > m[i]:
            out.append(1)
        else:
            out.append(-1 if allow_short else 0)
    return out


def s_momentum(bars: Sequence[Bar], n: int, allow_short: bool) -> List[Optional[int]]:
    c = [b.c for b in bars]
    out: List[Optional[int]] = []
    for i in range(len(bars)):
        if i < n:
            out.append(0)
            continue
        d = c[i] - c[i - n]
        out.append(1 if d > 0 else (-1 if (d < 0 and allow_short) else 0))
    return out


def s_donchian(bars: Sequence[Bar], n: int, exit_n: int) -> List[Optional[int]]:
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if i < max(n, exit_n):
            out.append(0)
            continue
        hi = max(b.h for b in bars[i - n:i])
        lo = min(b.l for b in bars[i - n:i])
        c = bars[i].c
        if pos == 0:
            pos = 1 if c > hi else (-1 if c < lo else 0)
        elif pos == 1 and c < min(b.l for b in bars[i - exit_n:i]):
            pos = -1 if c < lo else 0
        elif pos == -1 and c > max(b.h for b in bars[i - exit_n:i]):
            pos = 1 if c > hi else 0
        out.append(pos)
    return out


STRATEGIES: Dict[str, Callable[[Sequence[Bar]], List[Optional[int]]]] = {
    "B&H 买入持有": s_buy_hold,
    "SMA200 多头过滤": lambda b: s_sma_filter(b, 200, False),
    "SMA200 多空": lambda b: s_sma_filter(b, 200, True),
    "动量12 多头": lambda b: s_momentum(b, 12, False),
    "动量12 多空": lambda b: s_momentum(b, 12, True),
    "唐奇安 55/20 (海龟)": lambda b: s_donchian(b, 55, 20),
}


def main() -> int:
    summary: Dict[str, Dict[str, Tuple[float, float]]] = {}

    for tf in ("1d", "4h", "1h"):
        bars = load_bars(KLINES / f"BTCUSDT_{tf}.csv")
        bpy = BARS_PER_YEAR[tf]
        split = int(len(bars) * 0.70)
        print(f"\n{'=' * 96}")
        print(f"  BTCUSDT {tf}   {len(bars)} 根   "
              f"{datetime.fromtimestamp(bars[0].ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f" → {datetime.fromtimestamp(bars[-1].ts/1000, tz=timezone.utc):%Y-%m-%d}")
        print(f"  样本内 = 前 70% (至 {datetime.fromtimestamp(bars[split-1].ts/1000, tz=timezone.utc):%Y-%m-%d})"
              f"   样本外 = 后 30%")
        print(f"{'=' * 96}")
        print(f"{'策略':<22} | {'样本内: 收益    夏普     回撤   笔数    胜率':<44} | "
              f"{'样本外: 收益    夏普     回撤   笔数    胜率'}")
        print("-" * 96)

        for name, fn in STRATEGIES.items():
            sig = fn(bars)
            isr = backtest(bars, sig, bpy, 0, split)
            oos = backtest(bars, sig, bpy, split, len(bars))
            summary.setdefault(name, {})[tf] = (isr.sharpe, oos.sharpe)
            print(f"{name:<22} | {isr.row():<44} | {oos.row()}")

    print(f"\n{'=' * 96}")
    print("  跨周期一致性: 样本外夏普 (三种周期同号 = 更可能是真信号, 而非某段行情的巧合)")
    print(f"{'=' * 96}")
    print(f"{'策略':<22} | {'1d':>8} {'4h':>8} {'1h':>8} | {'同为正?':>10} {'三周期均值':>12}")
    print("-" * 96)
    for name, d in summary.items():
        vals = [d.get(tf, (0.0, 0.0))[1] for tf in ("1d", "4h", "1h")]
        allpos = all(v > 0 for v in vals)
        print(f"{name:<22} | {vals[0]:>8.2f} {vals[1]:>8.2f} {vals[2]:>8.2f} | "
              f"{('是' if allpos else '否'):>10} {statistics.mean(vals):>12.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
