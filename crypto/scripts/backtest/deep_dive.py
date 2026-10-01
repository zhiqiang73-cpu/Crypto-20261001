"""深度挖掘: 成交量冲击动量策略的参数稳健性 + 走步验证.

第一轮扫描的发现:
  * 均值回归 (RSI / 布林) 在 1d/4h/1h 上全线亏损 —— 高胜率但巨亏, 典型的
    "没止损地捡钢镚"。
  * 趋势跟随 (EMA交叉 / 唐奇安 / ATR通道) 在 4h/1d 正期望但样本外明显衰减,
    且跑不赢买入持有。
  * **成交量异常** 是唯一在三个周期上都保持"夏普最高 + 回撤最小"的家族,
    且在 4h 上样本内第 1、样本外也第 1。

本脚本要回答三个问题:
  Q1. 成交量冲击的参数是不是"一片平原"(稳健) 还是"一根尖刺"(过拟合)?
  Q2. 加上趋势过滤 / ATR 止损后是变好还是变坏?
  Q3. 走步验证 (5 折, 每折样本内挑参 → 样本外检验) 下, 它还能活吗?
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
KLINES = ROOT / "inputs" / "klines"
COST_PER_SIDE = 0.0005 + 0.0002


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
        for r in csv.DictReader(fh):
            try:
                out.append(Bar(int(r["open_time"]), float(r["open"]), float(r["high"]),
                               float(r["low"]), float(r["close"]), float(r["volume"])))
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda b: b.ts)
    return out


def sma_series(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr_series(bars: Sequence[Bar], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(bars)
    trs = [bars[0].h - bars[0].l]
    for i in range(1, len(bars)):
        pc = bars[i - 1].c
        trs.append(max(bars[i].h - bars[i].l, abs(bars[i].h - pc), abs(bars[i].l - pc)))
    run = 0.0
    for i, t in enumerate(trs):
        run += t
        if i >= n:
            run -= trs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


# --------------------------------------------------------------------------- 回测
@dataclass
class Stats:
    ret: float = 0.0
    cagr: float = 0.0
    sharpe: float = 0.0
    mdd: float = 0.0
    trades: int = 0
    win: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    bars: int = 0

    def key(self) -> str:
        return (f"{self.ret:>8.1%} {self.cagr:>7.1%} {self.sharpe:>6.2f} "
                f"{self.mdd:>7.1%} {self.trades:>6d} {self.win:>6.1%}")


def run_backtest(
    bars: Sequence[Bar],
    entries: Sequence[Optional[int]],   # 第 i 根收盘后产生的方向 (-1/0/1)
    hold: int,
    atr: Optional[Sequence[Optional[float]]] = None,
    stop_mult: float = 0.0,
    target_mult: float = 0.0,
    start: int = 0,
    end: Optional[int] = None,
    bars_per_year: float = 365 * 6,
) -> Stats:
    """下一根开盘进场; 持有 hold 根或触发 ATR 止损/止盈后离场."""
    end = len(bars) if end is None else end
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    rets: List[float] = []
    pnls: List[float] = []

    pos = 0
    entry_px = 0.0
    entry_i = -1
    stop_px = 0.0
    tgt_px = 0.0

    i = max(start, 1)
    while i < end - 1:
        if pos == 0:
            want = entries[i]
            if want:
                px = bars[i + 1].o
                pos, entry_px, entry_i = want, px, i + 1
                a = atr[i] if atr else None
                if a and stop_mult > 0:
                    stop_px = px - pos * stop_mult * a
                else:
                    stop_px = 0.0
                if a and target_mult > 0:
                    tgt_px = px + pos * target_mult * a
                else:
                    tgt_px = 0.0
                i += 1
                continue
            i += 1
            continue

        b = bars[i]
        exit_px: Optional[float] = None
        # 先判止损 (保守: 同一根内两者都触及时按止损算)
        if stop_px and ((pos == 1 and b.l <= stop_px) or (pos == -1 and b.h >= stop_px)):
            exit_px = stop_px
        elif tgt_px and ((pos == 1 and b.h >= tgt_px) or (pos == -1 and b.l <= tgt_px)):
            exit_px = tgt_px
        elif i - entry_i >= hold:
            exit_px = b.c

        if exit_px is not None:
            gross = (exit_px / entry_px - 1.0) * pos
            net = gross - 2 * COST_PER_SIDE
            equity *= (1.0 + net)
            pnls.append(net)
            pos, entry_px, entry_i, stop_px, tgt_px = 0, 0.0, -1, 0.0, 0.0
            rets.append(net)
            peak = max(peak, equity)
            mdd = max(mdd, 1.0 - equity / peak)
        i += 1

    if pos != 0 and entry_px:
        net = (bars[end - 1].c / entry_px - 1.0) * pos - 2 * COST_PER_SIDE
        equity *= (1.0 + net)
        pnls.append(net)

    n = len(rets)
    sharpe = 0.0
    if n > 1:
        m = sum(rets) / n
        sd = statistics.stdev(rets)
        if sd > 0:
            sharpe = m / sd * math.sqrt(bars_per_year / max(hold, 1))
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    years = max((bars[end - 1].ts - bars[start].ts) / 1000 / 86400 / 365, 1e-9)
    return Stats(
        ret=equity - 1.0,
        cagr=(equity ** (1 / years) - 1) if equity > 0 else -1.0,
        sharpe=sharpe, mdd=mdd, trades=len(pnls),
        win=(len(wins) / len(pnls)) if pnls else 0.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        bars=n,
    )


# --------------------------------------------------------------------------- 信号
def volume_shock_signals(
    bars: Sequence[Bar], vol_ma: Sequence[Optional[float]], mult: float,
    trend_ma: Optional[Sequence[Optional[float]]] = None,
    use_bar_direction: bool = True,
) -> List[Optional[int]]:
    out: List[Optional[int]] = [None] * len(bars)
    for i in range(len(bars)):
        ma = vol_ma[i]
        if ma is None or ma <= 0:
            continue
        if bars[i].v <= mult * ma:
            continue
        if use_bar_direction:
            d = 1 if bars[i].c > bars[i].o else (-1 if bars[i].c < bars[i].o else 0)
        else:
            d = 1 if i > 0 and bars[i].c > bars[i - 1].c else -1
        if d == 0:
            continue
        if trend_ma is not None:
            t = trend_ma[i]
            if t is None:
                continue
            if d == 1 and bars[i].c <= t:
                continue
            if d == -1 and bars[i].c >= t:
                continue
        out[i] = d
    return out


# --------------------------------------------------------------------------- Q1/Q2
def grid_search(bars: List[Bar], label: str, bpy: float) -> List[Tuple[str, Stats, Stats]]:
    split = int(len(bars) * 0.70)
    vols = [b.v for b in bars]
    vol_mas = {n: sma_series(vols, n) for n in (20, 50, 100)}
    trend_mas = {n: sma_series([b.c for b in bars], n) for n in (200,)}
    atrs = {n: atr_series(bars, n) for n in (14,)}

    rows: List[Tuple[str, Stats, Stats]] = []
    for vn in (20, 50, 100):
        for mult in (2.0, 2.5, 3.0, 4.0):
            for hold in (5, 10, 20, 40):
                for trend_n in (None, 200):
                    for stop_m in (0.0, 2.0):
                        sig = volume_shock_signals(
                            bars, vol_mas[vn], mult,
                            trend_mas[trend_n] if trend_n else None,
                        )
                        atr = atrs[14] if stop_m else None
                        isr = run_backtest(bars, sig, hold, atr, stop_m, 0.0, 0, split, bpy)
                        oos = run_backtest(bars, sig, hold, atr, stop_m, 0.0, split, len(bars), bpy)
                        if isr.trades < 20 or oos.trades < 8:
                            continue
                        name = (f"vol{vn} x{mult} hold{hold} "
                                f"{'趋势' if trend_n else '无过滤'}"
                                f"{' +ATR止损' if stop_m else ''}")
                        rows.append((name, isr, oos))

    print(f"\n{'=' * 108}")
    print(f"  Q1/Q2  {label}   样本内 {datetime.fromtimestamp(bars[0].ts/1000, tz=timezone.utc):%Y-%m}"
          f"→{datetime.fromtimestamp(bars[split-1].ts/1000, tz=timezone.utc):%Y-%m}"
          f" | 样本外 →{datetime.fromtimestamp(bars[-1].ts/1000, tz=timezone.utc):%Y-%m}")
    print(f"{'=' * 108}")
    print(f"{'组合':<40} | {'样本内: 收益  CAGR  夏普  回撤   笔数  胜率':<48} | 样本外夏普")
    print("-" * 108)
    rows.sort(key=lambda r: -r[2].sharpe)
    for name, isr, oos in rows[:24]:
        print(f"{name:<40} | {isr.key():<48} | {oos.sharpe:>6.2f}")
    return rows


# --------------------------------------------------------------------------- Q3
def walk_forward(bars: List[Bar], label: str, bpy: float, folds: int = 5) -> None:
    vols = [b.v for b in bars]
    vol_mas = {n: sma_series(vols, n) for n in (20, 50, 100)}
    trend_mas = {n: sma_series([b.c for b in bars], n) for n in (200,)}
    atrs = {n: atr_series(bars, n) for n in (14,)}

    combos = []
    for vn in (20, 50, 100):
        for mult in (2.0, 2.5, 3.0, 4.0):
            for hold in (5, 10, 20, 40):
                for trend_n in (None, 200):
                    for stop_m in (0.0, 2.0):
                        combos.append((vn, mult, hold, trend_n, stop_m))

    sig_cache: Dict[Tuple, List[Optional[int]]] = {}
    for c in combos:
        vn, mult, hold, trend_n, stop_m = c
        sig_cache[c] = volume_shock_signals(
            bars, vol_mas[vn], mult, trend_mas[trend_n] if trend_n else None)

    n = len(bars)
    # 扩窗走步: 把数据切成 folds+1 块, 第 k 折用前 k 块训练、第 k+1 块检验。
    # 这样每折的检验窗口等长, 且训练集只增不减 —— 上一版把检验窗口挤到只剩
    # 几根 K 线, 等于用噪声去平均, 会稀释真实结论。
    nblocks = folds + 1
    seg = n // nblocks
    print(f"\n{'=' * 108}")
    print(f"  Q3 扩窗走步验证  {label}   {folds} 折, 每折用前 k 块挑参 → 第 k+1 块检验 "
          f"(每块 {seg} 根, 挑参只看夏普)")
    print(f"{'=' * 108}")
    print(f"{'折':<5} {'训练区间':<24} {'测试区间':<24} {'选中参数':<34} "
          f"{'测试夏普':>8} {'测试收益':>9} {'笔数':>6}")
    print("-" * 108)

    oos_sharpes: List[float] = []
    oos_rets: List[float] = []
    for k in range(1, nblocks):
        tr_start = 0
        tr_end = k * seg
        te_end = min((k + 1) * seg, n)
        best = None
        best_s = -1e9
        for c in combos:
            vn, mult, hold, trend_n, stop_m = c
            st = run_backtest(bars, sig_cache[c], hold,
                              atrs[14] if stop_m else None, stop_m, 0.0,
                              tr_start, tr_end, bpy)
            if st.trades < 25:
                continue
            if st.sharpe > best_s:
                best_s, best = st.sharpe, c
        if best is None:
            continue
        vn, mult, hold, trend_n, stop_m = best
        te = run_backtest(bars, sig_cache[best], hold,
                          atrs[14] if stop_m else None, stop_m, 0.0,
                          tr_end, te_end, bpy)
        oos_sharpes.append(te.sharpe)
        oos_rets.append(te.ret)
        nm = (f"vol{vn} x{mult} hold{hold} "
              f"{'趋势' if trend_n else '无过滤'}{' +止损' if stop_m else ''}")
        print(f"{k:<5} "
              f"{datetime.fromtimestamp(bars[tr_start].ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f"→{datetime.fromtimestamp(bars[tr_end-1].ts/1000, tz=timezone.utc):%Y-%m-%d}   "
              f"{datetime.fromtimestamp(bars[tr_end].ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f"→{datetime.fromtimestamp(bars[te_end-1].ts/1000, tz=timezone.utc):%Y-%m-%d}   "
              f"{nm:<34} {te.sharpe:>8.2f} {te.ret:>9.1%} {te.trades:>6d}")

    if oos_sharpes:
        pos = sum(1 for s in oos_sharpes if s > 0)
        print("-" * 108)
        print(f"  样本外夏普: 均值 {statistics.mean(oos_sharpes):.2f}  "
              f"中位 {statistics.median(oos_sharpes):.2f}  "
              f"为正 {pos}/{len(oos_sharpes)} 折")
        print(f"  样本外收益: 均值 {statistics.mean(oos_rets):.1%}  "
              f"为正 {sum(1 for r in oos_rets if r > 0)}/{len(oos_rets)} 折")


def main() -> int:
    for tf, bpy in (("4h", 365 * 6), ("1h", 365 * 24)):
        bars = load_bars(KLINES / f"BTCUSDT_{tf}.csv")
        print(f"\n载入 BTCUSDT_{tf}.csv: {len(bars)} 根")
        grid_search(bars, tf, bpy)
        walk_forward(bars, tf, bpy, folds=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
