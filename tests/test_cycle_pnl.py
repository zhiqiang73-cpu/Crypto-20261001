"""完整持仓周期净收益配对测试。

覆盖 2026-10-04 审计发现的三个口径问题：
  1. 分批平仓被当成多笔交易 → 胜率/盈亏比虚高；
  2. 分层建仓的层数没有被识别；
  3. 反手（平旧仓 + 开新仓）在成交层面必须拆开。
另覆盖资金费归属、未平仓周期与覆盖度报告。
"""
from __future__ import annotations

import asyncio
import unittest

from review.cycle_pnl import (
    Cycle,
    attribute_funding,
    build_ledger,
    coverage,
    fetch_all_fills,
    pair_cycles,
    split_legs,
    summarize,
)

MS = 60_000
BASE = 1_700_000_000_000  # 真实毫秒基准：0 被当作「未设置」的哨兵，测试不能用 0


def fill(t, side, qty, price, *, pnl=0.0, fee=0.0, maker=True,
         order="o1", fid=None, symbol="BTCUSDT", position_side="BOTH"):
    return {
        "symbol": symbol,
        "id": fid if fid is not None else int(BASE + t),
        "orderId": order,
        "side": side,
        "qty": qty,
        "price": price,
        "realizedPnl": pnl,
        "commission": fee,
        "commissionAsset": "USDT",
        "time": BASE + t,
        "maker": maker,
        "positionSide": position_side,
    }


class SplitLegsTest(unittest.TestCase):
    def test_pure_open(self):
        legs, pos = split_legs(0.0, 1.0)
        self.assertEqual(legs, [("open", 1.0)])
        self.assertAlmostEqual(pos, 1.0)

    def test_pure_close(self):
        legs, pos = split_legs(1.0, -1.0)
        self.assertEqual(legs, [("close", -1.0)])
        self.assertAlmostEqual(pos, 0.0)

    def test_partial_close(self):
        legs, pos = split_legs(1.0, -0.3)
        self.assertEqual(legs, [("close", -0.3)])
        self.assertAlmostEqual(pos, 0.7)

    def test_add_same_direction(self):
        legs, pos = split_legs(0.5, 0.5)
        self.assertEqual(legs, [("open", 0.5)])
        self.assertAlmostEqual(pos, 1.0)

    def test_reversal_splits_close_then_open(self):
        legs, pos = split_legs(1.0, -2.0)
        self.assertEqual(legs, [("close", -1.0), ("open", -1.0)])
        self.assertAlmostEqual(pos, -1.0)


class PairingTest(unittest.TestCase):
    def test_simple_round_trip(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, fee=0.05, order="open1"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=10.0, fee=0.055,
                 maker=False, order="close1"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(open_cycles), 0)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertTrue(c.closed)
        self.assertEqual(c.side, 1)
        self.assertAlmostEqual(c.gross, 10.0)
        self.assertAlmostEqual(c.commission, 0.105)
        self.assertAlmostEqual(c.net, 10.0 - 0.105)
        self.assertAlmostEqual(c.open_notional, 100.0)
        self.assertEqual(c.open_orders, 1)
        self.assertEqual(c.close_orders, 1)
        self.assertAlmostEqual(c.hold_min, 60.0, msg='平仓在 60 分钟后')

    def test_partial_closes_count_as_one_trade(self):
        """分批平仓必须只算一笔交易 —— 这是审计的核心问题。"""
        rows = [
            fill(0, "BUY", 1.0, 100.0, fee=0.05, order="o"),
            fill(15 * MS, "SELL", 0.3, 105.0, pnl=1.5, fee=0.02, order="c1"),
            fill(30 * MS, "SELL", 0.7, 108.0, pnl=5.6, fee=0.04, order="c2"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(open_cycles), 0)
        self.assertEqual(len(cycles), 1, "三次成交腿仍只是一笔完整交易")
        c = cycles[0]
        self.assertAlmostEqual(c.gross, 7.1)
        self.assertEqual(c.close_orders, 2)

        summary = summarize(cycles)
        self.assertEqual(summary["trade_count"], 1)
        self.assertEqual(summary["wins"], 1)

    def test_layers_are_counted(self):
        """分层加仓：两次独立开仓委托 → 层数 2，但仍是一笔交易。"""
        rows = [
            fill(0, "BUY", 0.5, 100.0, fee=0.02, order="L1"),
            fill(15 * MS, "BUY", 0.5, 102.0, fee=0.02, order="L2"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=9.0, fee=0.05,
                 maker=False, order="X"),
        ]
        cycles, _ = pair_cycles(rows)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.open_orders, 2, "两层加仓应被识别")
        self.assertAlmostEqual(c.open_notional, 0.5 * 100 + 0.5 * 102)
        self.assertAlmostEqual(c.open_qty, 1.0)

    def test_reversal_produces_two_cycles(self):
        """反手 = 平掉累计层 + 反向开新仓，必须拆成两个周期。"""
        rows = [
            fill(0, "BUY", 1.0, 100.0, fee=0.05, order="L"),
            fill(60 * MS, "SELL", 2.0, 90.0, pnl=-10.0, fee=0.1,
                 maker=False, order="R"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(cycles), 2)
        closed = [c for c in cycles if c.closed]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].side, 1)
        self.assertAlmostEqual(closed[0].gross, -10.0)
        # 平仓腿的手续费只分走一半（2.0 成交里 1.0 用于平仓）。
        self.assertAlmostEqual(closed[0].commission, 0.05 + 0.05)

        self.assertEqual(len(open_cycles), 1)
        self.assertEqual(open_cycles[0].side, -1)
        self.assertAlmostEqual(open_cycles[0].open_qty, 1.0)
        self.assertAlmostEqual(open_cycles[0].open_notional, 90.0)

    def test_open_position_excluded_from_closed_stats(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, fee=0.05, order="L"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(open_cycles), 1)
        summary = summarize(cycles)
        self.assertEqual(summary["trade_count"], 0)
        self.assertEqual(summary["open_cycles"], 1)
        self.assertAlmostEqual(summary["open_qty"], 1.0)

    def test_win_rate_is_per_cycle_not_per_fill(self):
        """一次赢的周期拆成 3 笔平仓腿，不应被算成 3 次盈利。"""
        rows = [
            fill(0, "BUY", 1.0, 100.0, fee=0.0, order="L"),
            fill(10 * MS, "SELL", 0.4, 110.0, pnl=4.0, fee=0.0, order="c1"),
            fill(20 * MS, "SELL", 0.3, 110.0, pnl=3.0, fee=0.0, order="c2"),
            fill(30 * MS, "SELL", 0.3, 110.0, pnl=3.0, fee=0.0, order="c3"),
            # 第二笔交易是亏损的
            fill(40 * MS, "BUY", 1.0, 110.0, fee=0.0, order="L2"),
            fill(50 * MS, "SELL", 1.0, 100.0, pnl=-10.0, fee=0.0, order="c4"),
        ]
        cycles, _ = pair_cycles(rows)
        summary = summarize(cycles)
        self.assertEqual(summary["trade_count"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["win_rate"], 0.5)

    def test_hedge_mode_groups_separately(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, order="L", position_side="LONG"),
            fill(0, "SELL", 1.0, 100.0, order="S", position_side="SHORT"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=10.0, order="XL",
                 position_side="LONG"),
            fill(60 * MS, "BUY", 1.0, 90.0, pnl=10.0, order="XS",
                 position_side="SHORT"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(open_cycles), 0)
        self.assertEqual(len(cycles), 2)
        self.assertEqual({c.side for c in cycles}, {1, -1})

    def test_dust_position_tolerated(self):
        rows = [
            fill(0, "BUY", 0.3, 100.0, order="L"),
            fill(60 * MS, "SELL", 0.299999, 110.0, pnl=3.0, order="X"),
        ]
        cycles, open_cycles = pair_cycles(rows)
        self.assertEqual(len(open_cycles), 0)
        self.assertEqual(len(cycles), 1)


class LayerCountTest(unittest.TestCase):
    """层数必须按追价序列算：一次追价的多个 orderId 只算 1 层。"""

    def test_multi_order_chase_counts_as_one_layer(self):
        # 模拟真实情形：0.198 BTC 被拆成 2 个 orderId、间隔 9 秒
        rows = [
            fill(0, "BUY", 0.1392, 84989.40, order="O1"),
            fill(9 * 1000, "BUY", 0.0588, 85073.60, order="O2"),
        ]
        cycles, _ = pair_cycles(rows)
        self.assertEqual(cycles[0].open_orders, 1, "同一次追价只算 1 层")

    def test_separate_bars_count_as_separate_layers(self):
        rows = [
            fill(0, "BUY", 0.198, 85014.0, order="O1"),
            fill(75 * 60 * 1000, "BUY", 0.098, 85098.0, order="O2"),
        ]
        cycles, _ = pair_cycles(rows)
        self.assertEqual(cycles[0].open_orders, 2)

    def test_chase_then_next_bar_is_two_layers(self):
        # 追价 3 个 orderId（间隔 < 300s）+ 下一根 15m 的新层
        rows = [
            fill(0, "BUY", 0.0044, 85094.30, order="A"),
            fill(108 * 1000, "BUY", 0.0018, 85094.60, order="B"),
            fill(184 * 1000, "BUY", 0.0918, 85098.20, order="C"),
            fill(900 * 1000, "BUY", 0.1000, 85100.00, order="D"),
        ]
        cycles, _ = pair_cycles(rows)
        self.assertEqual(cycles[0].open_orders, 2)


class SourceTagTest(unittest.TestCase):
    """策略单归属：台账里没有的绝不猜成策略单。"""

    def _cycles(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, order="A1"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=10.0, order="A2"),
        ]
        return pair_cycles(rows)[0]

    def test_all_orders_known_is_strategy(self):
        from review.cycle_pnl import tag_source

        cycles = self._cycles()
        tag_source(cycles, {"A1", "A2"})
        self.assertEqual(cycles[0].source, "strategy")
        self.assertEqual(summarize(cycles)["by_source"]["strategy"]["trades"], 1)

    def test_unknown_orders_are_not_guessed(self):
        from review.cycle_pnl import tag_source

        cycles = self._cycles()
        tag_source(cycles, {"SOMETHING_ELSE"})
        self.assertEqual(cycles[0].source, "unattributed")

    def test_empty_ledger_marks_everything_unattributed(self):
        from review.cycle_pnl import tag_source

        cycles = self._cycles()
        tag_source(cycles, set())
        self.assertEqual(cycles[0].source, "unattributed")

    def test_partial_match_is_mixed(self):
        from review.cycle_pnl import tag_source

        rows = [
            fill(0, "BUY", 0.5, 100.0, order="L1"),
            fill(15 * MS, "BUY", 0.5, 101.0, order="L2"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=9.0, order="X"),
        ]
        cycles, _ = pair_cycles(rows)
        tag_source(cycles, {"L1"})
        self.assertEqual(cycles[0].source, "mixed")


class FundingTest(unittest.TestCase):
    def test_funding_attributed_to_open_cycle(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, order="L"),
            fill(120 * MS, "SELL", 1.0, 110.0, pnl=10.0, order="X"),
        ]
        cycles, _ = pair_cycles(rows)
        income = [{"symbol": "BTCUSDT", "income": -0.42, "time": BASE + 60 * MS}]
        attr = attribute_funding(cycles, income)
        self.assertAlmostEqual(attr["matched"], -0.42)
        self.assertAlmostEqual(attr["orphan"], 0.0)
        self.assertAlmostEqual(cycles[0].funding, -0.42)
        self.assertAlmostEqual(cycles[0].net, 10.0 - 0.42)

    def test_orphan_funding_is_reported_not_dropped(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, order="L"),
            fill(30 * MS, "SELL", 1.0, 110.0, pnl=10.0, order="X"),
        ]
        cycles, _ = pair_cycles(rows)
        income = [{"symbol": "BTCUSDT", "income": -0.9, "time": BASE + 600 * MS}]
        attr = attribute_funding(cycles, income)
        self.assertAlmostEqual(attr["orphan"], -0.9)
        summary = summarize(cycles, funding_orphan=attr["orphan"])
        # 归属不上的资金费仍进入净额，但单独标注。
        self.assertAlmostEqual(summary["net_pnl"], 10.0 - 0.9)
        self.assertAlmostEqual(summary["funding_orphan"], -0.9)


class SummarizeTest(unittest.TestCase):
    def test_unit_edge_uses_open_notional(self):
        c = Cycle(symbol="BTCUSDT", side=1, open_notional=1000.0,
                  gross=20.0, commission=10.0, closed=True)
        summary = summarize([c])
        self.assertAlmostEqual(summary["net_pnl"], 10.0)
        self.assertAlmostEqual(summary["unit_edge_bps"], 100.0)

    def test_drawdown_follows_close_order(self):
        a = Cycle(symbol="X", side=1, open_ms=0, close_ms=10,
                  gross=100.0, closed=True)
        b = Cycle(symbol="X", side=1, open_ms=20, close_ms=30,
                  gross=-250.0, closed=True)
        summary = summarize([a, b])
        self.assertAlmostEqual(summary["realized_max_drawdown"], 250.0)
        self.assertAlmostEqual(summary["realized_net_cum"], -150.0)

    def test_maker_taker_split(self):
        c = Cycle(symbol="X", side=1, gross=0.0, closed=True,
                  commission=0.06, maker_fee=0.02, taker_fee=0.04)
        summary = summarize([c])
        self.assertAlmostEqual(summary["maker_fee"], 0.02)
        self.assertAlmostEqual(summary["taker_fee"], 0.04)
        self.assertAlmostEqual(summary["commission"], 0.06)


class CoverageTest(unittest.TestCase):
    def test_truncation_flagged(self):
        rows = [fill(0, "BUY", 1.0, 100.0, order="L")]
        cycles, _ = pair_cycles(rows)
        cov = coverage(cycles, rows, truncated=True, start_ms=0)
        self.assertTrue(cov["truncated"])
        self.assertEqual(cov["fills"], 1)
        self.assertEqual(cov["open_cycles"], 1)

    def test_clipped_at_start_flagged_by_orphan_close(self):
        """窗口起点把持仓切成两半：带平仓盈亏却没有对应开仓腿。"""
        rows = [fill(10 * MS, "SELL", 1.0, 100.0, pnl=7.0, order="X")]
        cycles, _ = pair_cycles(rows)
        cov = coverage(cycles, rows, truncated=False, start_ms=0)
        self.assertEqual(cov["orphan_closes"], 1)
        self.assertTrue(cov["clipped_at_start"])

    def test_clean_window_not_flagged(self):
        rows = [
            fill(0, "BUY", 1.0, 100.0, order="L"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=10.0, order="X"),
        ]
        cycles, _ = pair_cycles(rows)
        cov = coverage(cycles, rows, truncated=False, start_ms=0)
        self.assertEqual(cov["orphan_closes"], 0)
        self.assertFalse(cov["clipped_at_start"])


class _FakeClient:
    """假客户端：记录每次调用的窗口参数，便于断言分窗行为。"""

    def __init__(self, pages, income_rows=None):
        self._pages = list(pages)
        self._income = income_rows or []
        self.calls = []

    async def user_trades(self, symbol=None, *, limit=50, start_time=None,
                          end_time=None, strict=False):
        self.calls.append((symbol, limit, start_time, end_time))
        if not self._pages:
            return []
        return self._pages.pop(0)

    async def income(self, symbol=None, *, income_type=None, limit=100,
                     start_time=None, end_time=None, strict=False):
        return list(self._income)


class FetchTest(unittest.TestCase):
    """分窗拉取：币安要求 startTime/endTime 跨度 ≤7 天，必须显式给 endTime。"""

    @staticmethod
    def _at(ms_abs, fid):
        row = fill(0, "BUY", 0.1, 100.0, fid=fid)
        row["time"] = ms_abs
        return row

    def test_windowed_pagination_dedupes(self):
        import time as _t

        now = int(_t.time() * 1000)
        page1 = [self._at(now - 9 * MS, 1),
                 self._at(now - 8 * MS, 2),
                 self._at(now - 7 * MS, 3)]
        page2 = [self._at(now - 7 * MS, 3),      # 与上一页重叠，必须去重
                 self._at(now - 6 * MS, 4)]
        client = _FakeClient([page1, page2])
        rows, truncated = asyncio.run(
            fetch_all_fills(client, "BTCUSDT", now - 12 * MS, page_limit=3)
        )
        self.assertFalse(truncated)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(set(ids)), "重复成交必须去重")
        self.assertEqual(len(rows), 4)
        # 第二页游标应为第一页最后一笔时间 +1ms（窗口内继续翻页）
        self.assertEqual(client.calls[1][2], now - 7 * MS + 1)

    def test_every_request_carries_bounded_window(self):
        import time as _t

        now = int(_t.time() * 1000)
        client = _FakeClient([[self._at(now - MS, 1)]])
        asyncio.run(fetch_all_fills(client, "BTCUSDT", now - 3 * 86_400_000,
                                    page_limit=100))
        for _sym, _lim, st, et in client.calls:
            self.assertIsNotNone(et, "必须显式给 endTime，否则跨度=起点到现在会报错")
            self.assertLessEqual(et - (st or et), 7 * 86_400_000,
                                 "跨度超过币安 7 天硬限制会直接报错")

    def test_no_start_uses_single_recent_page(self):
        client = _FakeClient([[fill(0, "BUY", 0.1, 100.0, fid=1)]])
        rows, truncated = asyncio.run(
            fetch_all_fills(client, "BTCUSDT", 0, page_limit=100)
        )
        self.assertEqual(len(rows), 1)
        self.assertFalse(truncated)
        self.assertEqual(len(client.calls), 1, "无起点时只需一次请求")

    def test_max_pages_marks_truncated(self):
        import time as _t

        now = int(_t.time() * 1000)
        # 每页时间递增，游标才会持续前进；max_pages=3 不足以取完。
        pages = []
        for k in range(5):
            t0 = now - 10 * MS + k * 2 * MS
            pages.append([self._at(t0, k * 10 + 1),
                          self._at(t0 + MS, k * 10 + 2)])
        client = _FakeClient(pages)
        _rows, truncated = asyncio.run(
            fetch_all_fills(client, "BTCUSDT", now - 3600_000,
                            page_limit=2, max_pages=3)
        )
        self.assertTrue(truncated)


class BuildLedgerTest(unittest.TestCase):
    def test_end_to_end_per_symbol_and_total(self):
        pages = [[
            fill(0, "BUY", 1.0, 100.0, fee=0.02, order="L"),
            fill(60 * MS, "SELL", 1.0, 110.0, pnl=10.0, fee=0.05,
                 maker=False, order="X"),
        ]]
        client = _FakeClient(
            pages,
            income_rows=[{"symbol": "BTCUSDT", "income": -0.1, "time": BASE + 30 * MS}],
        )
        out = asyncio.run(build_ledger(client, ["BTCUSDT"], 0))
        sym = out["per_symbol"]["BTCUSDT"]
        self.assertEqual(sym["trade_count"], 1)
        self.assertAlmostEqual(sym["net_pnl"], 10.0 - 0.07 - 0.1)
        self.assertAlmostEqual(sym["maker_fee"], 0.02)
        self.assertAlmostEqual(sym["taker_fee"], 0.05)
        self.assertAlmostEqual(out["total"]["net_pnl"], 10.0 - 0.07 - 0.1)
        self.assertEqual(out["errors"], {})


if __name__ == "__main__":
    unittest.main()
