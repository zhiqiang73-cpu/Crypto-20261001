#!/usr/bin/env python3
"""回放入口骨架：必须调用生产评分/交易规则，禁止另写有利策略。

当前能力：
- 价格路径上验证 pretrade / hard_sl（不宣称四面策略有效）
- 完整四面回放需要历史「当时可得」数据；缺失则标记 UNVERIFIABLE

用法（离线）:
  python3 -m scripts.replay_entry --help
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading.pretrade import PretradeLimits, recheck_entry


def replay_price_path(rows: list) -> dict:
    """rows: [{ts_ms, mark, signal_price?, side?, atr?}, ...]"""
    out = []
    for r in rows:
        if not r.get("side"):
            continue
        pre = recheck_entry(
            side=r["side"],
            signal_price=float(r.get("signal_price") or r["mark"]),
            signal_ts_ms=int(r.get("signal_ts_ms") or r["ts_ms"]),
            now_ms=int(r["ts_ms"]),
            exec_mark=float(r["mark"]),
            quote_ts_ms=int(r["ts_ms"]),
            atr=r.get("atr"),
            limits=PretradeLimits(max_adverse_atr=float(r.get("max_adverse_atr") or 0.5)),
        )
        out.append({"ts_ms": r["ts_ms"], "ok": pre.ok, "reasons": pre.reasons})
    return {
        "mode": "price_path_only",
        "note": "仅验证执行门控；不能宣称四面策略样本外有效",
        "n": len(out),
        "blocked": sum(1 for x in out if not x["ok"]),
        "rows": out,
        "strategy_oos_edge": "INSUFFICIENT_EVIDENCE",
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Production-rule replay (offline)")
    p.add_argument("--input", type=Path, help="JSONL price path")
    p.add_argument("--demo", action="store_true", help="run built-in adverse move demo")
    args = p.parse_args(argv)
    if args.demo:
        rows = [
            {"ts_ms": 1000, "mark": 100.0, "side": "LONG", "atr": 1.0, "signal_price": 100.0, "signal_ts_ms": 1000},
            {"ts_ms": 2000, "mark": 100.6, "side": "LONG", "atr": 1.0, "signal_price": 100.0, "signal_ts_ms": 1000},
            {"ts_ms": 3000, "mark": 102.0, "side": "LONG", "atr": 1.0, "signal_price": 100.0, "signal_ts_ms": 1000},
        ]
        print(json.dumps(replay_price_path(rows), ensure_ascii=False, indent=2))
        return 0
    if not args.input or not args.input.exists():
        print("需要 --input JSONL 或 --demo；四面完整回放数据不足时不得宣称通过")
        return 2
    rows = [json.loads(l) for l in args.input.read_text().splitlines() if l.strip()]
    print(json.dumps(replay_price_path(rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
