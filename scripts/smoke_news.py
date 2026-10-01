#!/usr/bin/env python3
"""消息面 + 链上烟雾测试."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from collectors.free_news import FreeNewsCollector
from collectors.free_onchain import FreeOnchainCollector
from mappers.news_mapper import NewsFactorMapper
from models.signals import StrategyHorizon


async def main() -> None:
    oc = FreeOnchainCollector()
    news = FreeNewsCollector()
    try:
        osnap = await oc.fetch_once()
        print("--- Onchain ---")
        print(f"  available={osnap.available} err={osnap.last_error}")
        print(f"  hashrate_chg={osnap.hashrate_ma30_change_pct}")
        print(f"  whale_net_btc={osnap.whale_net_flow_btc} dir={osnap.whale_transfer_direction}")
        print(f"  usdt_mint={osnap.usdt_mint_24h} burn={osnap.usdt_burn_24h}")

        usdt_net = None
        if osnap.usdt_mint_24h is not None or osnap.usdt_burn_24h is not None:
            usdt_net = (osnap.usdt_mint_24h or 0) - (osnap.usdt_burn_24h or 0)
        nsnap = await news.fetch_once(
            whale_institutional_score=osnap.whale_net_flow_btc,
            usdt_net_mint_24h=usdt_net,
        )
        print("--- News raw ---")
        print(f"  available={nsnap.available} err={nsnap.last_error}")
        print(f"  etf_daily={nsnap.etf_daily_net_usd} weekly={nsnap.etf_weekly_net_usd}")
        print(f"  dxy_5d={nsnap.dxy_change_5d} src={nsnap.dxy_source}")
        print(f"  halving_since={nsnap.months_since_halving:.1f}m")
        print(f"  regulation={nsnap.regulation_score} conf={nsnap.regulation_confidence}")

        result = NewsFactorMapper().map(nsnap, StrategyHorizon.SHORT_TERM)
        print("--- S_news ---")
        print(f"  {result.reasoning}")
        print(f"  sub={result.sub_scores}")
        print(f"  missing={result.missing_fields}")
    finally:
        await oc.close()
        await news.close()


if __name__ == "__main__":
    asyncio.run(main())
