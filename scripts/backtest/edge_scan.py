"""在真实 BTCUSDT 历史数据上做系统性策略扫描.

方法学要点 (缺一不可, 否则结果没有意义):
  1. **无未来函数**: 第 i 根收盘后才能算出信号, 在第 i+1 根开盘价成交。
  2. **扣真实成本**: 币安 U 本位永续 taker 0.05%, 再叠加滑点。每换一次方向都要付。
  3. **样本内 / 样本外分割**: 前 70% 用来挑参数, 后 30% 只用来看它是否还成立。
     只报样本内好看的策略等于自欺。
  4. **多空双向**: U 本位永续可以做空, 只做多会系统性漏掉一半行情。
  5. **基准对照**: 买入持有 (B&H) 是必须打败的对手。

输出: 每个策略在 IS / OOS 上的收益、夏普、最大回撤、交易次数、胜率。
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
KLINES = ROOT / "inputs" / "klines"

# --------------------------------------------------------------------------- 成本
TAKER_FEE = 0.0005      # 币安 U 本位永续 taker
SLIPPAGE = 0.0002       # 保守滑点估计
COST_PER_SIDE = TAKER_FEE + SLIPPAGE


# --------------------------------------------------------------------------- 数据
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
        rdr = csv.DictReader(fh)
        for r in rdr:
            try:
                out.append(Bar(
                    ts=int(r["open_time"]),
                    o=float(r["open"]), h=float(r["high"]),
                    l=float(r["low"]), c=float(r["close"]),
                    v=float(r["volume"]),
                ))
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda b: b.ts)
    return out


# --------------------------------------------------------------------------- 指标
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


def ema(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    k = 2.0 / (n + 1)
    prev: Optional[float] = None
    for i, v in enumerate(vals):
        prev = v if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / n, loss / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars: Sequence[Bar], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(bars)
    trs: List[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.h - b.l)
            continue
        pc = bars[i - 1].c
        trs.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    run = 0.0
    for i, t in enumerate(trs):
        run += t
        if i >= n:
            run -= trs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def stdev(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


# --------------------------------------------------------------------------- 回测
@dataclass
class Result:
    name: str
    ret: float
    cagr: float
    sharpe: float
    mdd: float
    trades: int
    win_rate: float
    bars: int


def backtest(bars: Sequence[Bar], target: Sequence[Optional[int]],
             name: str, start: int = 0, end: Optional[int] = None) -> Result:
    """target[i] 是第 i 根收盘后确定的目标仓位, 在第 i+1 根开盘执行."""
    end = len(bars) if end is None else end
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    pos = 0
    entry_px = 0.0
    rets: List[float] = []
    trades = 0
    wins = 0

    for i in range(max(start, 1), end - 1):
        want = target[i]
        if want is None:
            want = pos
        if want != pos:
            px = bars[i + 1].o
            # 平旧仓
            if pos != 0 and entry_px:
                gross = (px / entry_px - 1.0) * pos
                net = gross - 2 * COST_PER_SIDE
                equity *= (1.0 + net)
                trades += 1
                if net > 0:
                    wins += 1
            pos = want
            entry_px = px if pos != 0 else 0.0

        if i + 1 < end:
            nxt = bars[i + 1].c
            cur = bars[i + 1].o
            if pos != 0 and cur:
                rets.append((nxt / cur - 1.0) * pos)

    if pos != 0 and entry_px:
        px = bars[end - 1].c
        net = (px / entry_px - 1.0) * pos - 2 * COST_PER_SIDE
        equity *= (1.0 + net)
        trades += 1
        if net > 0:
            wins += 1

    # 回撤 (用逐根盯市近似)
    eq = 1.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, 1.0 - eq / peak)

    n = len(rets)
    if n > 1:
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        sd = math.sqrt(var)
        # 按年化: 传入的 rets 是每根 bar 的
        sharpe = (mean / sd * math.sqrt(365 * 24)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    years = max((bars[end - 1].ts - bars[start].ts) / 1000 / 86400 / 365, 1e-9)
    cagr = (equity ** (1 / years) - 1) if equity > 0 else -1.0
    return Result(
        name=name, ret=equity - 1.0, cagr=cagr, sharpe=sharpe, mdd=mdd,
        trades=trades, win_rate=(wins / trades if trades else 0.0), bars=n,
    )


# --------------------------------------------------------------------------- 策略
def strat_buy_hold(bars: Sequence[Bar]) -> List[Optional[int]]:
    return [1] * len(bars)


def strat_ema_cross(bars: Sequence[Bar], fast: int, slow: int) -> List[Optional[int]]:
    c = [b.c for b in bars]
    f, s = ema(c, fast), ema(c, slow)
    out: List[Optional[int]] = []
    for i in range(len(bars)):
        if f[i] is None or s[i] is None:
            out.append(0)
        else:
            out.append(1 if f[i] > s[i] else -1)
    return out


def strat_donchian(bars: Sequence[Bar], n: int, exit_n: int) -> List[Optional[int]]:
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if i < n:
            out.append(0)
            continue
        hi = max(b.h for b in bars[i - n:i])
        lo = min(b.l for b in bars[i - n:i])
        c = bars[i].c
        if pos == 0:
            if c > hi:
                pos = 1
            elif c < lo:
                pos = -1
        elif pos == 1:
            if i >= exit_n:
                ex = min(b.l for b in bars[i - exit_n:i])
                if c < ex:
                    pos = -1 if c < lo else 0
        elif pos == -1:
            if i >= exit_n:
                ex = max(b.h for b in bars[i - exit_n:i])
                if c > ex:
                    pos = 1 if c > hi else 0
        out.append(pos)
    return out


def strat_rsi_reversion(bars: Sequence[Bar], n: int, lo: float, hi: float) -> List[Optional[int]]:
    c = [b.c for b in bars]
    r = rsi(c, n)
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if r[i] is None:
            out.append(0)
            continue
        if pos == 0:
            if r[i] < lo:
                pos = 1
            elif r[i] > hi:
                pos = -1
        else:
            if (pos == 1 and r[i] > 50) or (pos == -1 and r[i] < 50):
                pos = 0
        out.append(pos)
    return out


def strat_bollinger_reversion(bars: Sequence[Bar], n: int, k: float) -> List[Optional[int]]:
    c = [b.c for b in bars]
    m, s = sma(c, n), stdev(c, n)
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if m[i] is None or s[i] is None or s[i] == 0:
            out.append(0)
            continue
        up, dn = m[i] + k * s[i], m[i] - k * s[i]
        if pos == 0:
            if c[i] < dn:
                pos = 1
            elif c[i] > up:
                pos = -1
        else:
            if (pos == 1 and c[i] >= m[i]) or (pos == -1 and c[i] <= m[i]):
                pos = 0
        out.append(pos)
    return out


def strat_atr_breakout(bars: Sequence[Bar], n: int, k: float) -> List[Optional[int]]:
    c = [b.c for b in bars]
    m, a = sma(c, n), atr(bars, n)
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if m[i] is None or a[i] is None:
            out.append(0)
            continue
        if pos == 0:
            if c[i] > m[i] + k * a[i]:
                pos = 1
            elif c[i] < m[i] - k * a[i]:
                pos = -1
        else:
            if (pos == 1 and c[i] < m[i]) or (pos == -1 and c[i] > m[i]):
                pos = 0
        out.append(pos)
    return out


def strat_volume_spike(bars: Sequence[Bar], n: int, mult: float) -> List[Optional[int]]:
    v = [b.v for b in bars]
    c = [b.c for b in bars]
    ma = sma(v, n)
    out: List[Optional[int]] = []
    pos = 0
    for i in range(len(bars)):
        if ma[i] is None or ma[i] == 0 or i < 1:
            out.append(0)
            continue
        up = c[i] > c[i - 1]
        if v[i] > mult * ma[i]:
            pos = 1 if up else -1
        elif pos != 0:
            # 持有 10 根后离场
            held = 0
            for j in range(i - 1, max(i - 12, -1), -1):
                if j < 0 or ma[j] is None or ma[j] == 0:
                    break
                if v[j] > mult * ma[j]:
                    held = i - j
                    break
            if held >= 10:
                pos = 0
        out.append(pos)
    return out


def strat_trend_filter(bars: Sequence[Bar], sma_n: int, mom_n: int) -> List[Optional[int]]:
    """只在长期均线上方且动量向上时做多, 下方且动量向下时做空."""
    c = [b.c for b in bars]
    m = sma(c, sma_n)
    out: List[Optional[int]] = []
    for i in range(len(bars)):
        if m[i] is None or i < mom_n:
            out.append(0)
            continue
        mom = c[i] - c[i - mom_n]
        if c[i] > m[i] and mom > 0:
            out.append(1)
        elif c[i] < m[i] and mom < 0:
            out.append(-1)
        else:
            out.append(0)
    return out


# --------------------------------------------------------------------------- 扫描
def fmt(r: Result) -> str:
    return (f"{r.name:<34} {r.ret:>10.1%} {r.cagr:>8.1%} {r.sharpe:>7.2f} "
            f"{r.mdd:>8.1%} {r.trades:>7d} {r.win_rate:>7.1%}")


HEADER = (f"{'策略':<34} {'累计':>10} {'CAGR':>8} {'夏普':>7} "
          f"{'最大回撤':>8} {'交易数':>7} {'胜率':>7}")


def run_suite(bars: List[Bar], label: str, start: int, end: int) -> List[Result]:
    results: List[Result] = []

    def add(name: str, target: List[Optional[int]]) -> None:
        results.append(backtest(bars, target, name, start, end))

    add("B&H 买入持有", strat_buy_hold(bars))
    for f, s in ((12, 48), (20, 80), (50, 200), (24, 96)):
        add(f"EMA交叉 {f}/{s}", strat_ema_cross(bars, f, s))
    for n, ex in ((20, 10), (55, 20), (100, 40)):
        add(f"唐奇安突破 {n}/{ex}", strat_donchian(bars, n, ex))
    for n, lo, hi in ((14, 30, 70), (14, 20, 80), (7, 25, 75)):
        add(f"RSI回归 {n} {lo}/{hi}", strat_rsi_reversion(bars, n, lo, hi))
    for n, k in ((20, 2.0), (20, 2.5), (50, 2.0)):
        add(f"布林回归 {n} {k}", strat_bollinger_reversion(bars, n, k))
    for n, k in ((20, 1.5), (50, 2.0), (100, 2.0)):
        add(f"ATR通道突破 {n} {k}", strat_atr_breakout(bars, n, k))
    for n, m in ((50, 3.0), (100, 3.0)):
        add(f"成交量异常 {n} {m}x", strat_volume_spike(bars, n, m))
    for s, mo in ((200, 24), (100, 12), (200, 48)):
        add(f"趋势过滤 SMA{s}/动量{mo}", strat_trend_filter(bars, s, mo))

    print(f"\n{'=' * 92}")
    print(f"  {label}   ({datetime.fromtimestamp(bars[start].ts/1000, tz=timezone.utc):%Y-%m-%d}"
          f" → {datetime.fromtimestamp(bars[end-1].ts/1000, tz=timezone.utc):%Y-%m-%d})")
    print(f"{'=' * 92}")
    print(HEADER)
    print("-" * 92)
    for r in sorted(results, key=lambda x: -x.sharpe):
        print(fmt(r))
    return results


def main() -> int:
    tf = sys.argv[1] if len(sys.argv) > 1 else "1d"
    path = KLINES / f"BTCUSDT_{tf}.csv"
    bars = load_bars(path)
    print(f"载入 {path.name}: {len(bars)} 根")

    split = int(len(bars) * 0.70)
    run_suite(bars, f"{tf} 样本内 (前 70%)", 0, split)
    run_suite(bars, f"{tf} 样本外 (后 30%)", split, len(bars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
