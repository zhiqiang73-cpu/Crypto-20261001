"""从离线事件 JSON 运行 BTC/ETH 组合回放；无交易客户端、无数据下载。

输入：{"config": {...}, "signals": [{"close_ms": ..., "leg_id": ..., ...}],
       "polls": [{"ms": ..., "mark": {"BTCUSDT": ..., "ETHUSDT": ...},
                  "current_atr_1h": {...}, "trade_px": {...},
                  "fill_fraction": {...} 或 "confirmed_fill"/"fill_id"/"commission_cash": {...}}]}
每根 signal 必须为已收盘的方向决策；成交/资金费是原始输入或明确模型情景。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from shadow.portfolio_replay import Poll, PortfolioReplay, ReplayConfig, Signal


def replay_file(input_path: Path, output_path: Path) -> dict:
    if "runtime" in output_path.resolve().parts:
        raise ValueError("回放结果禁止写入 runtime")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    cfg = ReplayConfig(**payload.get("config", {}))
    signals = [Signal(**row) for row in payload["signals"]]
    polls = [Poll(**row) for row in payload["polls"]]
    result = PortfolioReplay(cfg).run(signals, polls)
    out = {"scope": "offline_hypothetical_not_trading_or_verified_pnl",
           "config": asdict(cfg), "result": asdict(result)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)
                           + "\n", encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="本地已收盘信号/双标的轮询/成交/资金费 JSON")
    parser.add_argument("--output", type=Path, required=True,
                        help="离线情景结果 JSON；不能是 runtime")
    args = parser.parse_args()
    result = replay_file(args.input, args.output)
    summary = result["result"]
    print(json.dumps({"scope": result["scope"], "equity": summary["equity"],
                      "halted": summary["halted"], "fees": summary["fees"],
                      "funding": summary["funding"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
