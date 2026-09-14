#!/usr/bin/env python3
"""联调技术面: 拉 Binance K线 → 提取特征 → S_tech.

用法:
  python3 scripts/smoke_tech.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from collectors.binance_klines import BinanceKlinesCollector
from indicators.engine import extract_tech_features
from mappers.tech_mapper import TechFactorMapper
from models.signals import StrategyHorizon


async def main() -> None:
    col = BinanceKlinesCollector()
    try:
        intra, daily = await col.fetch_tech_inputs("15m", 300)
        print(f"intraday bars={len(intra)} daily={len(daily)}")
        print(f"last close={intra[-1].close} time={intra[-1].open_time_ms}")
        snap = extract_tech_features(intra, daily)
        result = TechFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        print("--- TechSnapshot ---")
        print(f"  structure={snap.structure_bias} score={snap.structure_score}")
        print(f"  ema21/50/200={snap.ema21}/{snap.ema50}/{snap.ema200} ema_score={snap.ema_score}")
        print(f"  adx={snap.adx} atr_pct={snap.atr_pct} boll_squeeze={snap.boll_squeeze}")
        print(f"  vwap={snap.vwap} vp_poc={snap.vp_poc} pdh/pdl={snap.pdh}/{snap.pdl}")
        print(f"  rsi={snap.rsi} rsi_div={snap.rsi_divergence_score} macd={snap.macd_score}")
        print("--- S_tech ---")
        print(f"  {result.reasoning}")
        print(f"  indicators={ {k:v for k,v in result.indicator_scores.items() if v!=0} }")
        ok = snap.available and snap.price and snap.ema21 is not None
        print(f"--- CHECK: {'PASS' if ok else 'FAIL'} ---")
        if not ok:
            sys.exit(1)
    finally:
        await col.close()


if __name__ == "__main__":
    asyncio.run(main())
