"""Task 3 reproducible 15m validation and cost stress report.

This script is research-only: local CSV inputs, no network, no runtime/live state.
Baseline fee is engine FEE_PER_SIDE=5 bps per side. Stress recomputes trade net
from recorded gross P&L under fee-per-side and adverse entry/exit slippage.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

# Allow execution from the isolated worktree root or from any caller cwd.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.engine import FEE_PER_SIDE, SPEC_15M, ShadowConfig, run_shadow_spec

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "inputs", "klines")
OUT = os.path.join(ROOT, ".work", "task3", "split_stress.json")
EQUITY = 1000.0
WINDOWS = {
    "train_2021_2024": ("2021-01-01", "2025-01-01"),
    "validation_2025": ("2025-01-01", "2026-01-01"),
    "oos_2026": ("2026-01-01", "2026-10-04"),
}
CONFIGS = {
    "no_stop": (None, None),
    "normal_stop_1.5": (1.5, None),
    "stop_1.5_plus_be_1.5": (1.5, 1.5),
}
STRESSES = {
    "baseline_5bps_side_0slip": (0.0005, 0.0),
    "fee_10bps_side_0slip": (0.0010, 0.0),
    "fee_5bps_side_2bps_slip": (0.0005, 0.0002),
    "fee_10bps_side_5bps_slip": (0.0010, 0.0005),
}


def load(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: int(r["open_time"]))
    return {k: [float(r[k]) if k not in ("open_time",) else int(r[k]) for r in rows]
            for k in ("open_time", "open", "high", "low", "close", "volume")}


def as_arrays(d):
    import numpy as np
    return {"ts": np.asarray(d["open_time"], dtype=np.int64),
            **{k: np.asarray(d[k], dtype=float) for k in ("open", "high", "low", "close", "volume")}}


def dt_ms(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def stress_trade(t, fee_side, slip):
    # Adverse slip is applied once on entry and once on exit, in the wrong
    # direction for the trade. Fee is charged on the slipped notional.
    entry = t.entry_px * (1.0 + t.side * slip)
    exit_px = t.exit_px * (1.0 - t.side * slip)
    gross = (exit_px - entry) * t.qty * t.side
    fees = (entry + exit_px) * t.qty * fee_side
    return gross - fees


def summarize(trades, start_ms, end_ms):
    ts = [t for t in trades if start_ms <= t.entry_ms < end_ms]
    gross = sum(t.gross for t in ts)
    fees = sum(t.entry_fee + t.exit_fee for t in ts)
    net = sum(t.net for t in ts)
    wins = [t.net for t in ts if t.net > 0]
    losses = [t.net for t in ts if t.net <= 0]
    curve = EQUITY
    peak = curve
    mdd = 0.0
    for t in sorted(ts, key=lambda x: x.exit_ms):
        curve += t.net
        peak = max(peak, curve)
        mdd = max(mdd, (peak - curve) / peak if peak else 0.0)
    return {
        "n": len(ts), "gross": gross, "baseline_fees": fees, "baseline_net": net,
        "avg_net": net / len(ts) if ts else 0.0,
        "win_rate": len(wins) / len(ts) if ts else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else None,
        "max_drawdown_trade_curve": mdd,
        "worst_net": min((t.net for t in ts), default=0.0),
        "stresses": {name: sum(stress_trade(t, fee, slip) for t in ts)
                      for name, (fee, slip) in STRESSES.items()},
        "exit_reasons": {reason: sum(1 for t in ts if t.exit_reason == reason)
                         for reason in sorted({t.exit_reason for t in ts})},
    }


def main():
    results = {"metadata": {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "data_files": {}, "fee_baseline_per_side": FEE_PER_SIDE,
        "equity": EQUITY, "windows": WINDOWS, "configs": CONFIGS,
        "stress_definitions": STRESSES,
        "code_note": "isolated worktree snapshot; engine and strategy cards hashed separately in report",
    }, "rows": []}
    for sym in ("BTCUSDT", "ETHUSDT"):
        p15 = os.path.join(DATA, f"{sym}_15m.csv")
        p1h = os.path.join(DATA, f"{sym}_1h.csv")
        results["metadata"]["data_files"][sym] = {
            "15m": os.path.basename(p15), "1h": os.path.basename(p1h),
            "15m_rows": sum(1 for _ in open(p15)) - 1,
            "1h_rows": sum(1 for _ in open(p1h)) - 1,
        }
        b15, b1h = as_arrays(load(p15)), as_arrays(load(p1h))
        for cfg_name, (stop, be) in CONFIGS.items():
            spec = replace(SPEC_15M, stop_atr_mult=stop,
                           break_even_trigger_atr_mult=be,
                           halt_on_drawdown=True)
            spec_nohalt = replace(spec, halt_on_drawdown=False)
            for mode in ("A", "B"):
                for halt_name, run_spec in (("with_permanent_halt", spec), ("no_halt_research", spec_nohalt)):
                    res = run_shadow_spec(
                        b15, b1h, run_spec,
                        ShadowConfig(equity0=EQUITY, fixed_risk_equity=EQUITY,
                                     record_bars=False),
                    )
                    trades = res.trades_a if mode == "A" else res.trades_b
                    for win, (start, end) in WINDOWS.items():
                        row = {"symbol": sym, "config": cfg_name, "mode": mode,
                               "risk_gate": halt_name, "window": win,
                               **summarize(trades, dt_ms(start), dt_ms(end))}
                        row["halt_count_total"] = len(res.halts)
                        row["first_halt_utc"] = (datetime.fromtimestamp(res.halts[0]["ts"] / 1000, timezone.utc).isoformat()
                                                  if res.halts else None)
                        results["rows"].append(row)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(OUT)
    print(json.dumps(results["rows"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
