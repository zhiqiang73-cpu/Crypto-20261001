#!/usr/bin/env python3
"""实时评分演示 (免费源): S_data + S_tech → partial_CS.

用法:
  python3 scripts/live_score.py [秒数] [刷新间隔秒]

例:
  python3 scripts/live_score.py 60 15
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from runtime.live_loop import LiveScoringLoop


def _print(snap) -> None:
    cs = snap.composite_score if snap.is_full_cs else snap.partial_cs
    label = "CS" if snap.is_full_cs else "partial_CS"
    print(
        f"\n[{snap.timestamp_ms}] mark={snap.mark_price}\n"
        f"  S_news={snap.s_news}  S_data={snap.s_data:+.1f}  "
        f"S_tech={snap.s_tech:+.1f}  S_pred={snap.s_prediction}\n"
        f"  {label}={cs:+.1f} → {snap.decision.value}  "
        f"full={snap.is_full_cs} override={snap.overridden} valve={snap.safety_valve}\n"
        f"  missing={snap.missing_dimensions}\n"
        f"  {snap.reasoning}"
    )


async def main(duration: float = 45.0, refresh: float = 15.0) -> None:
    loop = LiveScoringLoop(refresh_sec=refresh)
    stop = asyncio.Event()

    async def _timer():
        await asyncio.sleep(duration)
        stop.set()
        loop.stop()

    timer = asyncio.create_task(_timer())
    try:
        await loop.run(stop_event=stop, on_score=_print)
    finally:
        timer.cancel()
        try:
            await timer
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
    ref = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    asyncio.run(main(dur, ref))
