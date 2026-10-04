"""运行器健壮性回归 — 2026-10-04 夜班故障的六条修复。

当晚的真实链条（测试对应用户可见的每一环）：
  1. 一笔永远开不出的委托（保证金不足 -2019）让 `place_limit_chase` 抛
     `TimeoutError`；而 `_request` 只捕获 `aiohttp.ClientError`，超时直接逃逸。
  2. 异常冒泡到 `step()`，整个 tick 中断 → 另一标的（ETH）不再处理、
     `save_state` 不执行 → 连续 6 次、每次 35~64 分钟的服务降级。
  3. 虚拟账本在委托**之前**就改写，委托失败也不回滚 → 账本与交易所长期脱节。
  4. 每 tick 都重发这笔注定被拒的单（单 tick 被动挂 25+ 次），既空转又触发超时。
  5. 停机漏掉的交叉只在 CSV 留痕、无任何告警 → ETH 漏两次反手，空单持了 8 小时。
  6. 日志里只有 196 条空消息 `异常: `，无类型无堆栈，无法排障。

所有客户端调用均为内存桩，不连接交易所、不写 runtime/。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import shadow.deploy as deploy
from shadow.deploy import (process_symbol, step, sync_net,
                           symbol_book_has_position)
from shadow.strategy_books import (SPECS, SPEC_15M, apply_virtual_signal,
                                   book_signed_qty, empty_book, entry_layers,
                                   layer_count, reconcile_symbol_books)
from trading.binance_client import BinanceTestnetClient
from trading.models import OrderResult, OrderState


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- 桩
class _FakeSession:
    """aiohttp session 的最小替身：请求一发起就超时。"""

    closed = False

    def request(self, *args, **kwargs):
        raise asyncio.TimeoutError()


class _ChaseRaiseStub(BinanceTestnetClient):
    """追价内部第一步就超时的客户端，用于验证「契约：永不抛」。"""

    def __init__(self):
        super().__init__(api_key="stub-key", api_secret="stub-secret",
                         base_url="https://testnet.binancefuture.com")

    async def _lot_step(self, symbol=None):
        raise asyncio.TimeoutError()


def _state_with(book_entry):
    st = {"strategies": {spec.id: empty_book() for spec in SPECS}}
    st["strategies"]["kdj15"]["entry"] = json.loads(json.dumps(book_entry))
    return st


def _long_entry(qty=0.5):
    return {"side": 1, "qty": qty, "px": 84_000.0, "atr": 200.0,
            "ms": 1_790_000_000_000, "reason": "首层", "layers": []}


class _OrderStub:
    """sync_net / process_symbol 用的交易所替身。"""

    def __init__(self, *, quantity=0.0, mark=84_000.0, available=10_000.0,
                 chase_ok=False, chase_filled=0.0, chase_error="rejected"):
        self.quantity = quantity
        self.mark = mark
        self.available = available
        self.chase_ok = chase_ok
        self.chase_filled = chase_filled
        self.chase_error = chase_error
        self.chase_calls = []

    async def get_position(self, symbol=None):
        side = "FLAT" if not self.quantity else (
            "LONG" if self.quantity > 0 else "SHORT")
        return SimpleNamespace(quantity=abs(self.quantity), side=side,
                               entry_price=0.0, mark_price=self.mark)

    async def get_balance(self):
        return SimpleNamespace(total_wallet_balance=5_000.0,
                               available_balance=self.available,
                               total_unrealized_pnl=0.0)

    async def mark_price(self, symbol=None):
        return self.mark

    async def get_position_settings(self, symbol=None):
        return {"leverage": 10, "margin_type": "ISOLATED"}

    async def place_limit_chase(self, **kwargs):
        self.chase_calls.append(kwargs)
        # 成交要反映到"交易所持仓"上，否则后续的账本对齐会（正确地）判为空仓。
        if self.chase_ok and self.chase_filled and not kwargs.get("reduce_only"):
            sign = 1.0 if kwargs.get("side") == "LONG" else -1.0
            self.quantity = sign * self.chase_filled
        return SimpleNamespace(
            ok=self.chase_ok, symbol=kwargs.get("symbol"),
            cum_filled_qty=self.chase_filled, avg_price=self.mark,
            requested_qty=kwargs.get("quantity"), error=self.chase_error,
            order_id="", client_order_id="", raw={}, order_state="CANCELED",
        )

    async def all_orders(self, *args, **kwargs):
        return []


# --------------------------------------------------------------- 1. 超时不再逃逸
class TestRequestTimeoutIsConverted(unittest.TestCase):
    def test_client_timeout_becomes_client_error(self):
        from trading.binance_client import BinanceClientError

        client = BinanceTestnetClient(
            api_key="k", api_secret="s",
            base_url="https://testnet.binancefuture.com",
        )
        client._session = _FakeSession()      # 不触发真实建连
        client._time_synced = True
        with self.assertRaises(BinanceClientError) as ctx:
            run(client._request("GET", "/fapi/v1/ping"))
        self.assertIn("TimeoutError", str(ctx.exception))


class TestPlaceLimitChaseNeverRaises(unittest.TestCase):
    def test_timeout_inside_chase_is_a_failed_result_not_an_exception(self):
        client = _ChaseRaiseStub()
        res = run(client.place_limit_chase(side="LONG", quantity=0.1,
                                           symbol="BTCUSDT"))
        self.assertFalse(res.ok)
        self.assertEqual(res.cum_filled_qty, 0.0)
        self.assertIn("chase_error", res.error)
        self.assertIn("TimeoutError", res.error)
        self.assertEqual(res.order_state, OrderState.UNKNOWN.value)


# ----------------------------------------------------------------- 2. tick 隔离
class TestStepIsolatesSymbolFailures(unittest.TestCase):
    def test_one_symbol_failing_does_not_skip_the_other_nor_the_save(self):
        seen, saved = [], []

        async def fake_process_symbol(client, st, symbol, **kwargs):
            seen.append(symbol)
            if symbol == "BTCUSDT":
                raise asyncio.TimeoutError()

        async def fake_step_sleep(_seconds):
            raise AssertionError("不应进入 sleep")

        client = _OrderStub()
        st = {"strategies": {spec.id: empty_book() for spec in SPECS}}
        with patch.object(deploy, "process_symbol", fake_process_symbol), \
                patch.object(deploy, "save_state",
                             lambda s: saved.append(s.copy())), \
                patch.object(deploy, "save_heartbeat", lambda **k: None), \
                patch.object(deploy, "consume_resume_requests", lambda s: []), \
                patch.object(deploy, "migrate_state", lambda s: None):
            run(step(client, st, execute=False))

        # BTC 抛异常后，ETH 仍必须被处理 —— 这正是当晚被跳过的那个标的。
        self.assertEqual(seen, ["BTCUSDT", "ETHUSDT"])
        # 而且状态必须落盘（当晚整轮丢弃、state 冻结数小时）。
        self.assertGreaterEqual(len(saved), 2)


# ------------------------------------------------ 2b. 按余额给数量封顶（预算制）
class TestMarginBudgetClamp(unittest.TestCase):
    """风险定量只回答"想下多少"；容量上限回答"能下多少"；取小。

    权益 5000、预算 70%、10x、价格 100 ⇒ 单标的整仓名义上限 35000 ⇒ 数量上限 350。
    """

    EQUITY, PX = 5_000.0, 100.0
    CAP = deploy.floor_step(5_000.0 * deploy.MARGIN_BUDGET_PER_TRADE * 10 / 100.0)

    def _clamp(self, qty, *, entry=None, side=1):
        book = {"entry": entry}
        return deploy.apply_margin_budget(
            book, qty, side=side, equity=self.EQUITY, px=self.PX)

    def test_below_cap_is_untouched(self):
        qty, note = self._clamp(100.0)
        self.assertEqual(qty, 100.0)
        self.assertEqual(note, "")

    def test_above_cap_is_clamped(self):
        qty, note = self._clamp(999.0)
        self.assertAlmostEqual(qty, self.CAP, places=6)
        self.assertIn("保证金预算封顶", note)

    def test_same_direction_add_only_gets_the_remaining_room(self):
        entry = {"side": 1, "qty": 100.0}
        qty, note = self._clamp(300.0, entry=entry, side=1)
        self.assertAlmostEqual(qty, self.CAP - 100.0, places=6)
        self.assertIn("保证金预算封顶", note)

    def test_full_position_leaves_no_room_for_another_layer(self):
        entry = {"side": 1, "qty": self.CAP}
        qty, _ = self._clamp(100.0, entry=entry, side=1)
        self.assertEqual(qty, 0.0)

    def test_reverse_gets_the_whole_cap_not_the_leftover(self):
        """反手会先清掉旧层，所以额度应按整仓算，不能被旧仓占掉。"""
        entry = {"side": -1, "qty": self.CAP}
        qty, _ = self._clamp(999.0, entry=entry, side=1)
        self.assertAlmostEqual(qty, self.CAP, places=6)

    def test_budget_zero_disables_the_clamp(self):
        """两层预算都设为 0 才完全不封顶；任一层 >0 就仍然生效。"""
        old = deploy.MARGIN_BUDGET_PER_TRADE
        old_port = deploy.PORTFOLIO_MARGIN_BUDGET
        deploy.MARGIN_BUDGET_PER_TRADE = 0.0
        deploy.PORTFOLIO_MARGIN_BUDGET = 0.0
        try:
            qty, note = self._clamp(999.0)
        finally:
            deploy.MARGIN_BUDGET_PER_TRADE = old
            deploy.PORTFOLIO_MARGIN_BUDGET = old_port
        self.assertEqual(qty, 999.0)
        self.assertEqual(note, "")

    def test_portfolio_budget_alone_still_clamps(self):
        """单标的预算为 0 时，组合预算仍必须挡住超额。"""
        old = deploy.MARGIN_BUDGET_PER_TRADE
        deploy.MARGIN_BUDGET_PER_TRADE = 0.0
        try:
            qty, note = self._clamp(999.0)
        finally:
            deploy.MARGIN_BUDGET_PER_TRADE = old
        self.assertAlmostEqual(qty, 400.0, places=6)
        self.assertIn("组合预算封顶", note)


# ------------------------------------------------------- 3. 下单失败必须回滚账本
class TestLedgerRollbackOnFailedOrder(unittest.TestCase):
    def _run_symbol(self, chase_ok, chase_filled, entry, *, mutate=True):
        client = _OrderStub(quantity=0.0, chase_ok=chase_ok,
                            chase_filled=chase_filled)
        bars = {"ts": [1_790_000_000_000], "open": [84_000.0],
                "high": [84_010.0], "low": [83_990.0], "close": [84_000.0]}
        atr = np.array([200.0])
        pack = {"15m": (bars, atr), "5m": (bars, atr)}

        def fake_process_strategy(spec, book, *a, **kw):
            # 模拟「信号已写入账本」——真实代码里这一步发生在下单之前。
            if mutate:
                book["entry"] = _long_entry()
            return mutate

        with patch.object(deploy, "detect_external",
                          lambda *a, **kw: _noop_coro(None)), \
                patch.object(deploy, "_fetch_symbol_bars",
                             lambda symbol, now: pack), \
                patch.object(deploy, "process_strategy", fake_process_strategy), \
                patch.object(deploy, "watch_for", lambda st, s: {}), \
                patch.object(deploy, "save_state", lambda s: None):
            st = _state_with(entry)
            run(process_symbol(client, st, "BTCUSDT", execute=True,
                               block=False, equity=5_000.0, now=1_790_000_000_001))
            return st

    def test_failed_order_zero_fill_restores_the_book(self):
        st = self._run_symbol(chase_ok=False, chase_filled=0.0, entry=None)
        # 委托零成交 → 账本必须还原为空仓，绝不能记成已开多。
        self.assertIsNone(st["strategies"]["kdj15"]["entry"])

    def test_successful_fill_keeps_the_book(self):
        st = self._run_symbol(chase_ok=True, chase_filled=0.5, entry=None)
        self.assertIsNotNone(st["strategies"]["kdj15"]["entry"])

    def test_ghost_position_is_cleared_when_exchange_is_flat(self):
        """账本记着仓位、交易所空仓、委托又开不出来 ⇒ 必须清零，不能留幽灵仓。

        2026-10-04 的 BTC：账本 SHORT 0.382、交易所 FLAT，每 tick 白跑一次同步。
        """
        st = self._run_symbol(chase_ok=False, chase_filled=0.0,
                              entry=_long_entry(), mutate=False)
        self.assertIsNone(st["strategies"]["kdj15"]["entry"])


async def _noop_coro(value):
    return value


# --------------------------------------------------------- 4. 保证金可行性前置校验
class TestMarginPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_log = deploy.TRADE_LOG
        self._old_out = deploy.OUT
        deploy.OUT = self.tmp.name
        deploy.TRADE_LOG = os.path.join(self.tmp.name, "trades.csv")

    def tearDown(self):
        deploy.TRADE_LOG = self._old_log
        deploy.OUT = self._old_out
        self.tmp.cleanup()

    def test_insufficient_margin_skips_the_order_entirely(self):
        st = _state_with(_long_entry(qty=0.5))
        client = _OrderStub(quantity=0.0, mark=84_000.0, available=100.0)
        res = run(sync_net(client, st, True, symbol="BTCUSDT", tag="kdj"))
        # 0.5 × 84000 / 10x = 4200 保证金 > 可用 100 → 一单都不该发。
        self.assertEqual(client.chase_calls, [])
        self.assertFalse(res.ok)
        self.assertTrue(res.error.startswith("skipped_margin_insufficient"))
        self.assertEqual(res.order_state, "skipped_margin")

    def test_sufficient_margin_still_places_the_order(self):
        st = _state_with(_long_entry(qty=0.5))
        client = _OrderStub(quantity=0.0, mark=84_000.0,
                            available=10_000.0, chase_ok=True, chase_filled=0.5)
        res = run(sync_net(client, st, True, symbol="BTCUSDT", tag="kdj"))
        self.assertEqual(len(client.chase_calls), 1)
        self.assertTrue(res.ok)

    def test_reduce_only_is_never_blocked_by_margin(self):
        """平仓/减仓不占新保证金，必须放行（否则灾难止损会被自己的检查挡住）。"""
        st = _state_with(None)          # 账本空仓 → desired 0
        client = _OrderStub(quantity=0.5, mark=84_000.0, available=0.0,
                            chase_ok=True, chase_filled=0.5)
        run(sync_net(client, st, True, symbol="BTCUSDT", tag="kdj"))
        self.assertEqual(len(client.chase_calls), 1)


# ------------------------------------------------------------ 5. 漏信号必须告警
class TestMissedSignalIsSurfaced(unittest.TestCase):
    def test_missed_bar_with_a_cross_warns_and_is_recorded(self):
        tmp = tempfile.TemporaryDirectory()
        old_reading, old_log = deploy.READING, deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "reading.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        try:
            n = 6
            step_ms = SPEC_15M.interval_ms
            ts = [1_790_000_000_000 + i * step_ms for i in range(n)]
            bars = {"ts": ts,
                    "open": [84_000.0] * n, "high": [84_010.0] * n,
                    "low": [83_990.0] * n, "close": [84_000.0] * n}
            atr = np.array([200.0] * n)
            # j=3 死叉、j=4 金叉；MACD 柱恒为正 → 只有 j=4 是有效交叉。
            k = np.array([50.0, 50.0, 50.0, 30.0, 60.0, 60.0])
            d = np.array([50.0] * n)
            book = empty_book()
            book["armed"] = True
            book["last_ts"] = ts[2]     # 停机 → 应补记 [3, 4]
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    patch("shadow.deploy.macd",
                          return_value=(np.zeros(n), np.zeros(n), np.ones(n))):
                deploy.process_strategy(SPEC_15M, book, bars, atr,
                                        equity=5_000.0, block=False,
                                        now=ts[-1] + step_ms, execute=False)
            self.assertEqual(book["missed_signals"], 1)
            # 告警必须留下可查的痕迹，而不是只打一行 stdout。
            self.assertEqual(book["missed_signal_last_ms"], ts[4])
            with open(deploy.TRADE_LOG, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("BTC 15m错过", text)
            self.assertIn("金叉做多", text)
        finally:
            deploy.READING, deploy.TRADE_LOG = old_reading, old_log
            tmp.cleanup()


# ------------------------------------------------ 5b. 部分成交必须对齐账本
class TestPartialFillReconciliation(unittest.TestCase):
    """账本在委托之前写意图；只成交一部分时账本会虚高，必须按实际净仓对齐。

    2026-10-04 ETH：目标 -15.854、实际只成交到 -7.430，账本却一直记着 -15.854。
    """

    def _state(self, *layers):
        book = empty_book()
        for qty, px, ms in layers:
            apply_virtual_signal(book, sig_long=True, sig_short=False, qty=qty,
                                 px=px, atr=10.0, ms=ms, block=False,
                                 min_qty=0.001, max_layers=3,
                                 meta={"symbol": "BTCUSDT"})
        st = {"strategies": {spec.id: empty_book() for spec in SPECS}}
        st["strategies"]["kdj15"] = book
        return st, book

    def test_matching_net_is_a_noop(self):
        st, book = self._state((6.0, 100.0, 1), (4.0, 110.0, 2))
        self.assertIsNone(reconcile_symbol_books(st, "BTCUSDT", 10.0))
        self.assertAlmostEqual(book_signed_qty(book), 10.0, places=6)

    def test_partial_fill_trims_the_newest_layer(self):
        st, book = self._state((6.0, 100.0, 1), (4.0, 110.0, 2))
        note = reconcile_symbol_books(st, "BTCUSDT", 7.0)
        self.assertIn("部分成交对齐", note)
        self.assertAlmostEqual(book_signed_qty(book), 7.0, places=6)
        # 老层不动，最新层被削到 1.0（且仍是 2 层）
        self.assertEqual(layer_count(book["entry"]), 2)
        self.assertAlmostEqual(entry_layers(book["entry"])[0]["qty"], 6.0, places=6)
        self.assertAlmostEqual(entry_layers(book["entry"])[-1]["qty"], 1.0, places=6)

    def test_partial_fill_can_drop_a_whole_layer(self):
        st, book = self._state((6.0, 100.0, 1), (4.0, 110.0, 2))
        reconcile_symbol_books(st, "BTCUSDT", 5.0)
        self.assertAlmostEqual(book_signed_qty(book), 5.0, places=6)
        self.assertEqual(layer_count(book["entry"]), 1)

    def test_exchange_flat_clears_the_book(self):
        st, book = self._state((6.0, 100.0, 1))
        note = reconcile_symbol_books(st, "BTCUSDT", 0.0)
        self.assertIn("归零", note)
        self.assertTrue(symbol_book_has_position(st, "BTCUSDT") is False)

    def test_exchange_holding_more_is_not_guessed_away(self):
        st, book = self._state((6.0, 100.0, 1))
        note = reconcile_symbol_books(st, "BTCUSDT", 9.0)
        self.assertIn("请人工复核", note)
        self.assertAlmostEqual(book_signed_qty(book), 6.0, places=6)

    def test_opposite_direction_is_not_guessed_away(self):
        st, book = self._state((6.0, 100.0, 1))
        note = reconcile_symbol_books(st, "BTCUSDT", -2.0)
        self.assertIn("方向不符", note)
        self.assertAlmostEqual(book_signed_qty(book), 6.0, places=6)


# ------------------------------------------- 5c. 追价期间的安全检查点（不阻塞盲区）
class TestChaseSafetyCheckpoint(unittest.TestCase):
    """追价最长 180 秒且占住主循环；检查点让调用方有机会中止它。"""

    def _stub(self, *, fill=True):
        class _Stub(BinanceTestnetClient):
            def __init__(self):
                super().__init__(api_key="stub-key", api_secret="stub-secret",
                                 base_url="https://testnet.binancefuture.com")
                self.placed = 0
                self.fill = fill

            async def _lot_step(self, symbol=None):
                return 0.001

            async def price_tick(self, symbol=None):
                return 0.1

            async def book_ticker(self, symbol=None):
                return {"bid": 100.0, "ask": 100.1}

            async def place_limit_order(self, side, quantity, price, symbol=None,
                                        **kw):
                self.placed += 1
                filled = quantity if self.fill else 0.0
                state = (OrderState.FILLED if filled > 0 else OrderState.CANCELED)
                return OrderResult(
                    ok=filled > 0, symbol=symbol, side=side, quantity=filled,
                    requested_qty=quantity, submitted_qty=quantity,
                    cum_filled_qty=filled, avg_price=price, status=state.value,
                    client_order_id=kw.get("client_order_id", "c"),
                    order_state=state.value, error="", raw={},
                )

        return _Stub()

    def test_checkpoint_can_abort_the_chase_before_any_order(self):
        client = self._stub()
        seen = []

        async def on_step(bid, ask):
            seen.append((bid, ask))
            return True                      # 要求中止

        res = run(client.place_limit_chase(side="LONG", quantity=0.5,
                                           symbol="BTCUSDT", on_step=on_step))
        self.assertEqual(seen, [(100.0, 100.1)])
        self.assertFalse(res.ok)
        self.assertIn("chase_aborted", res.error)
        # 关键：中止发生在挂单之前，不会留下任何遗留委托
        self.assertEqual(client.placed, 0)

    def test_checkpoint_returning_false_lets_the_order_through(self):
        client = self._stub()

        async def on_step(bid, ask):
            return False

        res = run(client.place_limit_chase(side="LONG", quantity=0.5,
                                           symbol="BTCUSDT", on_step=on_step))
        self.assertTrue(res.ok)
        self.assertEqual(client.placed, 1)

    def test_checkpoint_failure_must_not_break_ordering(self):
        client = self._stub()

        async def on_step(bid, ask):
            raise RuntimeError("检查点自己炸了")

        res = run(client.place_limit_chase(side="LONG", quantity=0.5,
                                           symbol="BTCUSDT", on_step=on_step))
        self.assertTrue(res.ok)
        self.assertEqual(client.placed, 1)


# ------------------------------------------------- 6. 逐仓未实现盈亏不再恒为 0
class TestIsolatedUnrealizedPnl(unittest.TestCase):
    """逐仓账户 crossUnPnl 恒 0，必须回落到 positionRisk。

    ⚠ 币安的真实字段名是 **unRealizedProfit**（大写 R）。第一版修复写成了小写
    `unrealizedProfit`，而**测试的假数据也是按小写编的** → 生产恒为 0、测试却
    全绿。这个用例因此显式钉住真实拼法，并顺带覆盖小写变体。
    """

    def _balance_with(self, risk_rows):
        client = BinanceTestnetClient(
            api_key="k", api_secret="s",
            base_url="https://testnet.binancefuture.com",
        )

        async def fake_request(method, path, params=None, **kw):
            if path.endswith("/balance"):
                return [{"asset": "USDT", "balance": "4451.29",
                         "availableBalance": "204.25", "crossUnPnl": "0"}]
            if path.endswith("/positionRisk"):
                return risk_rows
            raise AssertionError(path)

        client._request = fake_request
        return run(client.get_balance())

    def test_real_field_spelling_unRealizedProfit(self):
        bal = self._balance_with([
            {"symbol": "ETHUSDT", "unRealizedProfit": "61.24768006"},
            {"symbol": "BTCUSDT", "unRealizedProfit": "0"},
        ])
        self.assertAlmostEqual(bal.total_unrealized_pnl, 61.2477, places=3)
        self.assertAlmostEqual(bal.available_balance, 204.25, places=2)

    def test_negative_pnl_and_lowercase_variant_also_accepted(self):
        bal = self._balance_with([
            {"symbol": "ETHUSDT", "unRealizedProfit": None,
             "unrealizedProfit": "-33.01"},
        ])
        self.assertAlmostEqual(bal.total_unrealized_pnl, -33.01, places=2)

    def test_missing_field_is_zero_not_an_error(self):
        bal = self._balance_with([{"symbol": "ETHUSDT", "positionAmt": "1"}])
        self.assertEqual(bal.total_unrealized_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()
