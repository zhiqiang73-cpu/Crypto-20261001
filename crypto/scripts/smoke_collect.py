#!/usr/bin/env python3
"""联调: Binance 微观结构 + 免费衍生品源 (替代收费 CoinGlass).

用法:
  python3 scripts/smoke_collect.py [seconds]

默认用 Binance/Bybit/OKX 免费接口。
若设置了可用的 COINGLASS_API_KEY 且套餐够用, 仍会尝试 CoinGlass 作对照。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from collectors.binance_ws import BinanceFuturesCollector
from collectors.coinglass import CoinGlassCollector
from collectors.free_derivatives import FreeDerivativesCollector
from mappers.data_mapper import DataFactorMapper
from models.signals import StrategyHorizon
from models.snapshots import DataSnapshot
from utils.scoring import detect_session_zone


async def main(duration_sec: float = 20.0) -> None:
    bn = BinanceFuturesCollector()
    free = FreeDerivativesCollector()
    cg = CoinGlassCollector()
    mapper = DataFactorMapper()
    stop = asyncio.Event()

    print(f"COINGLASS_API_KEY status: {cg.key_status}")
    print("derivatives primary source: free (Binance OI/LSR/forceOrder + Bybit/OKX OI)")

    bn_task = asyncio.create_task(bn.run(stop_event=stop))
    free_task = asyncio.create_task(
        free.run(
            stop_event=stop,
            mark_price_provider=lambda: bn.snapshot.mark_price,
        )
    )
    cg_task = None
    if cg.has_api_key:
        cg_task = asyncio.create_task(
            cg.run(
                stop_event=stop,
                mark_price_provider=lambda: bn.snapshot.mark_price,
            )
        )

    print(f"collecting for {duration_sec:.0f}s ...")
    elapsed = 0.0
    while elapsed < duration_sec:
        step = min(5.0, duration_sec - elapsed)
        await asyncio.sleep(step)
        elapsed += step
        s = bn.snapshot
        f = free.snapshot
        print(
            f"  t={elapsed:.0f}s mark={s.mark_price} funding_ann={s.funding_rate_annualized} "
            f"ratio={s.bid_ask_ratio_2pct} synced={s.orderbook_synced} "
            f"oi={f.open_interest_usd} lsr={f.long_short_ratio}"
        )

    stop.set()
    bn.stop()
    free.stop()
    cg.stop()

    tasks = [bn_task, free_task]
    if cg_task:
        tasks.append(cg_task)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    except (asyncio.TimeoutError, Exception) as exc:
        print(f"shutdown note: {exc}")

    # 若免费源尚未拉到, 再强制 fetch 一次
    if not free.snapshot.available:
        await free.fetch_once(mark_price=bn.snapshot.mark_price)

    bsnap = bn.get_snapshot()
    fsnap = free.get_snapshot()
    csnap = cg.get_snapshot() if cg.has_api_key else None

    # 优先用免费源填数据面
    data = DataSnapshot(
        binance=bsnap,
        coinglass=fsnap,
        session=detect_session_zone(bsnap.event_time_ms),
        timestamp_ms=bsnap.event_time_ms or int(time.time() * 1000),
    )
    result = mapper.map(data, StrategyHorizon.SHORT_TERM)

    print("--- Binance micro ---")
    print(f"  mark={bsnap.mark_price} funding_ann={bsnap.funding_rate_annualized}")
    print(f"  bid/ask_2pct={bsnap.bid_ask_ratio_2pct} synced={bsnap.orderbook_synced}")
    print("--- Free derivatives ---")
    print(f"  available={fsnap.available} oi_usd={fsnap.open_interest_usd}")
    print(f"  oi_5m={fsnap.oi_change_5m_pct} oi_24h={fsnap.oi_change_24h_pct}")
    print(f"  lsr={fsnap.long_short_ratio}")
    print(f"  liq_5m long/short/total={fsnap.liq_long_5m_usd}/{fsnap.liq_short_5m_usd}/{fsnap.liq_total_5m_usd}")
    print(f"  heatmap_magnet={fsnap.heatmap_magnet}")
    if free.last_error:
        print(f"  last_error={free.last_error}")
    if csnap is not None:
        print("--- CoinGlass (optional) ---")
        print(f"  available={csnap.available} error={cg.last_error}")
    print("--- S_data ---")
    print(f"  {result.reasoning}")
    print(f"  funding={result.indicator_scores.get('funding_rate')} "
          f"oi={result.indicator_scores.get('open_interest')} "
          f"lsr={result.indicator_scores.get('long_short_ratio')} "
          f"ob={result.indicator_scores.get('orderbook_depth')}")

    ok_mark = bsnap.mark_price is not None and bsnap.mark_price > 0
    ok_fund = bsnap.funding_rate_annualized is not None
    ok_book = bsnap.bid_ask_ratio_2pct is not None
    ok_oi = fsnap.open_interest_usd is not None
    ok_lsr = fsnap.long_short_ratio is not None
    print("--- CHECK ---")
    print(f"  mark_price: {'PASS' if ok_mark else 'FAIL'}")
    print(f"  funding: {'PASS' if ok_fund else 'FAIL'}")
    print(f"  orderbook_ratio: {'PASS' if ok_book else 'FAIL'}")
    print(f"  orderbook_synced: {'PASS' if bsnap.orderbook_synced else 'WARN'}")
    print(f"  free_oi: {'PASS' if ok_oi else 'FAIL'}")
    print(f"  free_lsr: {'PASS' if ok_lsr else 'FAIL'}")
    print(f"  free_liq_stream: {'PASS' if (fsnap.liq_total_5m_usd or 0) > 0 else 'WARN (quiet window OK)'}")
    if csnap is not None:
        if csnap.available:
            print("  coinglass: PASS")
        else:
            print(f"  coinglass: FAIL/LOCKED ({cg.last_error})")
    if not (ok_mark and ok_fund and ok_book and ok_oi and ok_lsr):
        sys.exit(1)


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    asyncio.run(main(secs))
