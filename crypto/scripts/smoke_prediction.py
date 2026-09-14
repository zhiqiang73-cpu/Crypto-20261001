#!/usr/bin/env python3
"""预测面烟雾测试: Polymarket + Deribit → S_prediction."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from collectors.deribit import DeribitCollector
from collectors.polymarket import PolymarketCollector
from mappers.prediction_mapper import PredictionFactorMapper
from models.signals import StrategyHorizon
from models.snapshots import PredictionSnapshot


async def main() -> None:
    pm = PolymarketCollector()
    der = DeribitCollector()
    try:
        psnap = await pm.fetch_once(mark_price=95000)
        dsnap = await der.fetch_once()
        print("--- Polymarket ---")
        print(f"  available={psnap.available} err={psnap.last_error}")
        print(f"  btc_prob={psnap.btc_prob} change_1h={psnap.btc_prob_change_1h}")
        print(f"  slug={psnap.btc_market_slug} thr={psnap.btc_threshold_usd}")
        print(f"  fed_cut={psnap.fed_cut_prob} hike={psnap.fed_hike_prob} slug={psnap.fed_market_slug}")
        print("--- Deribit ---")
        print(f"  available={dsnap.available} err={dsnap.last_error}")
        print(f"  max_pain={dsnap.max_pain} dist={dsnap.max_pain_distance} iv={dsnap.iv} exp={dsnap.expiry}")
        pred = PredictionSnapshot(
            polymarket=psnap,
            max_pain_distance=dsnap.max_pain_distance,
            available=psnap.available or dsnap.max_pain_distance is not None,
        )
        result = PredictionFactorMapper().map(pred, StrategyHorizon.SHORT_TERM)
        print("--- S_prediction ---")
        print(f"  {result.reasoning}")
        print(f"  sub={result.sub_scores}")
        print(f"  missing={result.missing_fields}")
    finally:
        await pm.close()
        await der.close()


if __name__ == "__main__":
    asyncio.run(main())
