"""日志与报告输出 —— 逐根日志、逐笔日志、日报、周报."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from shadow.engine import BarRow, ShadowResult, Trade

BAR_COLS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "K", "D", "J", "ATR_1H", "UP", "MB", "LB", "带宽", "目标距离", "倍数",
    "MACD柱",
    "金叉", "死叉", "触发做多", "触发做空", "宽松做多", "宽松做空",
    "持仓方向", "持仓数量",
]
TRADE_COLS = [
    "模式", "方向", "开仓时间", "开仓价", "数量", "开仓手续费",
    "平仓时间", "平仓价", "平仓手续费", "持仓时长_分钟", "毛盈亏", "净盈亏",
    "开仓时_K", "开仓时_D", "开仓时_ATR_1H", "开仓时_带宽", "开仓时_倍数",
    "开仓时_手续费点数", "平仓原因",
]


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _yn(b: bool) -> str:
    return "Y" if b else ""


def write_bar_log(res: ShadowResult, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(BAR_COLS)
        for b in res.bars:
            w.writerow([
                _fmt_ts(b.ts), f"{b.o:.2f}", f"{b.h:.2f}", f"{b.l:.2f}", f"{b.c:.2f}",
                f"{b.v:.4f}", f"{b.k:.4f}", f"{b.d:.4f}", f"{b.j:.4f}",
                f"{b.atr_1h:.4f}", f"{b.up:.4f}", f"{b.mb:.4f}", f"{b.lb:.4f}",
                f"{b.bandwidth:.4f}", f"{b.target_dist:.4f}", f"{b.multiple:.4f}",
                f"{b.macd_hist:.4f}",
                _yn(b.gold_cross), _yn(b.dead_cross), _yn(b.sig_long), _yn(b.sig_short),
                _yn(b.loose_long), _yn(b.loose_short),
                b.pos_side, f"{b.pos_qty:.4f}",
            ])


def _trade_row(t: Trade) -> list:
    return [
        t.mode, t.side, _fmt_ts(t.entry_ms), f"{t.entry_px:.2f}", f"{t.qty:.4f}",
        f"{t.entry_fee:.4f}", _fmt_ts(t.exit_ms), f"{t.exit_px:.2f}",
        f"{t.exit_fee:.4f}", f"{t.hold_min:.1f}", f"{t.gross:.4f}", f"{t.net:.4f}",
        f"{t.k_at_entry:.4f}", f"{t.d_at_entry:.4f}", f"{t.atr_at_entry:.4f}",
        f"{t.bandwidth_at_entry:.4f}", f"{t.multiple_at_entry:.4f}",
        f"{t.fee_points_at_entry:.4f}", t.exit_reason,
    ]


def write_trade_log(res: ShadowResult, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(res.trades_a + res.trades_b, key=lambda t: t.entry_ms)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(TRADE_COLS)
        for t in rows:
            w.writerow(_trade_row(t))


def write_skip_log(res: ShadowResult, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["时间", "方向", "原因", "倍数"])
        for s in res.skips:
            w.writerow([_fmt_ts(s["ts"]), s["side"], s["reason"],
                        f"{s.get('multiple', float('nan')):.4f}"])


def _day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_reports(res: ShadowResult, outdir: str, equity0: float) -> List[str]:
    """每天一份 CSV + 一份人类可读日报。"""
    os.makedirs(outdir, exist_ok=True)
    bars_by_day: Dict[str, List[BarRow]] = {}
    for b in res.bars:
        bars_by_day.setdefault(_day_key(b.ts), []).append(b)
    trades_by_day: Dict[str, List[Trade]] = {}
    for t in res.trades_a + res.trades_b:
        trades_by_day.setdefault(_day_key(t.exit_ms), []).append(t)

    written = []
    for day in sorted(bars_by_day):
        bars = bars_by_day[day]
        trades = trades_by_day.get(day, [])
        # 当日 CSV
        csv_path = os.path.join(outdir, f"{day}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(BAR_COLS)
            for b in bars:
                w.writerow([
                    _fmt_ts(b.ts), f"{b.o:.2f}", f"{b.h:.2f}", f"{b.l:.2f}",
                    f"{b.c:.2f}", f"{b.v:.4f}", f"{b.k:.4f}", f"{b.d:.4f}",
                    f"{b.j:.4f}", f"{b.atr_1h:.4f}", f"{b.up:.4f}", f"{b.mb:.4f}",
                    f"{b.lb:.4f}", f"{b.bandwidth:.4f}", f"{b.target_dist:.4f}",
                    f"{b.multiple:.4f}", f"{b.macd_hist:.4f}",
                    _yn(b.gold_cross), _yn(b.dead_cross),
                    _yn(b.sig_long), _yn(b.sig_short), _yn(b.loose_long),
                    _yn(b.loose_short), b.pos_side, f"{b.pos_qty:.4f}",
                ])

        n_long = sum(1 for b in bars if b.sig_long)
        n_short = sum(1 for b in bars if b.sig_short)
        n_ll = sum(1 for b in bars if b.loose_long)
        n_ls = sum(1 for b in bars if b.loose_short)
        ta = [t for t in trades if t.mode == "A"]
        tb = [t for t in trades if t.mode == "B"]
        net_a = sum(t.net for t in ta)
        net_b = sum(t.net for t in tb)
        fee = sum(t.entry_fee + t.exit_fee for t in trades)
        wins = [t for t in ta if t.net > 0]

        txt = os.path.join(outdir, f"{day}.md")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write(f"# 影子模式日报 · {day} (UTC)\n\n")
            fh.write(f"- K 线根数: {len(bars)}\n")
            fh.write(f"- 严格做多信号: {n_long}   严格做空信号: {n_short}\n")
            fh.write(f"- 宽松做多信号: {n_ll}   宽松做空信号: {n_ls}\n")
            fh.write(f"- 模式 A 平仓笔数: {len(ta)}   净盈亏: {net_a:+.4f} USDT\n")
            fh.write(f"- 模式 B 平仓笔数: {len(tb)}   净盈亏: {net_b:+.4f} USDT\n")
            fh.write(f"- 手续费合计: {fee:.4f} USDT\n")
            if ta:
                fh.write(f"- 模式 A 胜率: {len(wins)/len(ta):.1%}\n")
            fh.write(f"- 当日最后一根 K 线持仓: "
                     f"{'多' if bars[-1].pos_side == 1 else ('空' if bars[-1].pos_side == -1 else '空仓')}"
                     f" {bars[-1].pos_qty:.4f}\n")
            fh.write(f"- 起始权益: {equity0:.2f} USDT\n")
        written.append(day)
    return written


def weekly_summary(res: ShadowResult, outdir: str, equity0: float) -> str:
    """每周一份汇总: 信号总数、成交数、通过率、胜率、平均盈亏比、总手续费占比。"""
    os.makedirs(outdir, exist_ok=True)

    def week_of(ms: int) -> str:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        return monday.strftime("%Y-%m-%d")

    weeks: Dict[str, dict] = {}
    for b in res.bars:
        w = weeks.setdefault(week_of(b.ts), {"bars": 0, "sig": 0, "loose": 0,
                                             "ta": [], "tb": [], "skips": 0})
        w["bars"] += 1
        w["sig"] += int(b.sig_long) + int(b.sig_short)
        w["loose"] += int(b.loose_long) + int(b.loose_short)
    for t in res.trades_a:
        weeks.setdefault(week_of(t.entry_ms), {"bars": 0, "sig": 0, "loose": 0,
                                               "ta": [], "tb": [], "skips": 0})["ta"].append(t)
    for t in res.trades_b:
        weeks.setdefault(week_of(t.entry_ms), {"bars": 0, "sig": 0, "loose": 0,
                                               "ta": [], "tb": [], "skips": 0})["tb"].append(t)
    for s in res.skips:
        weeks.setdefault(week_of(s["ts"]), {"bars": 0, "sig": 0, "loose": 0,
                                            "ta": [], "tb": [], "skips": 0})["skips"] += 1

    path = os.path.join(outdir, "weekly_summary.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# 影子模式周报汇总\n\n")
        fh.write(f"- 起始权益: {equity0:.2f} USDT\n")
        fh.write(f"- 模式 A 最终权益: {res.final_equity_a:.4f} USDT "
                 f"({(res.final_equity_a/equity0 - 1):+.2%})\n")
        fh.write(f"- 模式 B 最终权益: {res.final_equity_b:.4f} USDT "
                 f"({(res.final_equity_b/equity0 - 1):+.2%})\n\n")
        fh.write("| 周(周一) | K线 | 严格信号 | 宽松信号 | A成交 | A通过率 | A胜率 | "
                 "A平均盈亏比 | A净盈亏 | B成交 | B净盈亏 | 总手续费 | 手续费占比 |\n")
        fh.write("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for wk in sorted(weeks):
            v = weeks[wk]
            ta, tb = v["ta"], v["tb"]
            wins = [t for t in ta if t.net > 0]
            losses = [t for t in ta if t.net <= 0]
            wr = len(wins) / len(ta) if ta else 0.0
            aw = sum(t.net for t in wins) / len(wins) if wins else 0.0
            al = sum(t.net for t in losses) / len(losses) if losses else 0.0
            ratio = (aw / abs(al)) if al else float("nan")
            net_a = sum(t.net for t in ta)
            net_b = sum(t.net for t in tb)
            fee = sum(t.entry_fee + t.exit_fee for t in ta + tb)
            gross = sum(abs(t.gross) for t in ta + tb)
            fee_share = (fee / gross) if gross else float("nan")
            fh.write(f"| {wk} | {v['bars']} | {v['sig']} | {v['loose']} | {len(ta)} | "
                     f"{len(ta)/v['sig'] if v['sig'] else 0:.1%} | {wr:.1%} | "
                     f"{ratio:.2f} | {net_a:+.4f} | {len(tb)} | {net_b:+.4f} | "
                     f"{fee:.4f} | {fee_share:.1%} |\n")
    return path
