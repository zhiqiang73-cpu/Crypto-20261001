"""订单安全故障注入：仅内存桩；不读密钥、不联网、不写 runtime。"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shadow import deploy
from shadow.execution_safety import ExpansionBlocked, check_expansion
from shadow.strategy_books import next_net_leg
from tests.test_limit_chase import _PassiveStub
from tests.test_reconcile_and_limit_order import _StubBinance, _raw
from trading.binance_client import BinanceClientError, BinanceTestnetClient
from trading.models import AccountBalance, ManagedOrder, OrderResult, OrderState


def run(coro):
    return asyncio.run(coro)


def book():
    return {"bid": 100.0, "ask": 100.1}


def balance(available=500.0, wallet=1000.0):
    return AccountBalance(total_wallet_balance=wallet, available_balance=available,
                          total_unrealized_pnl=0.0)


class TestExpansionPreflight(unittest.TestCase):
    def check(self, **overrides):
        kw = dict(balance=balance(), risks=[], symbol="BTCUSDT", exchange_signed=0,
                  quantity=1.0, book=book(), leverage=10, min_qty=0.001)
        kw.update(overrides)
        return check_expansion(**kw)

    def test_good_expansion_reports_budget(self):
        out = self.check()
        self.assertGreater(out["gross_after"], out["gross_before"])
        self.assertLess(out["required_margin"], out["available"])

    def test_zero_available_does_not_fall_back_to_wallet(self):
        with self.assertRaisesRegex(ExpansionBlocked, "可用保证金不足"):
            self.check(balance=balance(available=0, wallet=1000))

    def test_other_symbols_count_toward_gross_cap(self):
        risks = [{"symbol": "ETHUSDT", "positionAmt": "20", "markPrice": "250",
                  "notional": "5000"}]
        with self.assertRaisesRegex(ExpansionBlocked, "总名义敞口超限"):
            self.check(risks=risks)

    def test_margin_buffer_and_reserve(self):
        with self.assertRaisesRegex(ExpansionBlocked, "保证金不足"):
            self.check(balance=balance(available=10))

    def test_stale_account_snapshot_rejected(self):
        risks = [{"symbol": "BTCUSDT", "positionAmt": "0.2", "markPrice": "100"}]
        with self.assertRaisesRegex(ExpansionBlocked, "仓位快照不一致"):
            self.check(risks=risks)

    def test_bad_mark_or_incomplete_data_fail_closed(self):
        with self.assertRaises(ExpansionBlocked):
            self.check(risks=[{"symbol": "ETHUSDT", "positionAmt": "1", "markPrice": "nan"}])
        with self.assertRaises(ExpansionBlocked):
            self.check(balance=SimpleNamespace(total_wallet_balance=1000,
                                               available_balance=None,
                                               total_unrealized_pnl=0))

    def test_zero_available_balance_parser(self):
        c = BinanceTestnetClient(api_key="stub", api_secret="stub")
        async def request(*args, **kwargs):
            return [{"asset": "USDT", "balance": "1000", "availableBalance": "0"}]
        c._request = request
        self.assertEqual(run(c.get_balance()).available_balance, 0)

    def test_account_snapshot_includes_isolated_loss_and_requires_fields(self):
        c = BinanceTestnetClient(api_key="stub", api_secret="stub")
        async def valid(*args, **kwargs):
            return {"totalWalletBalance": "1000", "availableBalance": "500",
                    "totalUnrealizedProfit": "-250"}
        c._request = valid
        snapshot = run(c.get_account_balance_snapshot())
        self.assertEqual(snapshot.total_unrealized_pnl, -250)
        async def incomplete(*args, **kwargs):
            return {"totalWalletBalance": "1000"}
        c._request = incomplete
        with self.assertRaises(BinanceClientError):
            run(c.get_account_balance_snapshot())


class TestLegPlan(unittest.TestCase):
    def test_reversal_closes_old_first_both_directions(self):
        self.assertEqual(next_net_leg(0.02, -0.01, 0.001), ("SHORT", 0.02, True))
        self.assertEqual(next_net_leg(-0.02, 0.01, 0.001), ("LONG", 0.02, True))
        self.assertEqual(next_net_leg(0, -0.01, 0.001), ("SHORT", 0.01, False))

    def test_reduce_only_and_minimum_residue(self):
        self.assertEqual(next_net_leg(0.005, 0, 0.001), ("SHORT", 0.005, True))
        self.assertIsNone(next_net_leg(5.2595, 5.259, 0.001))
        self.assertIsNone(next_net_leg(0.0005, 0, 0.001))


class FakeExchange:
    """净仓状态由已确认成交量更新；CID 先写入回调才准提交。"""
    def __init__(self, ex=0.0, *, available=500.0):
        self.ex = ex
        self.available = available
        self.calls = []
        self.query_state = OrderState.UNKNOWN
        self.query_fill = 0.0
        self.result_state = OrderState.FILLED
        self.partial = None
        self.error = ""
        self.opens = []
        self.other_opens = []
        self.fail_snapshot = False
        self.drift_before_order = 0.0
        self.upnl = 0.0

    async def get_position(self, symbol=None):
        side = "LONG" if self.ex > 0 else "SHORT" if self.ex < 0 else "FLAT"
        return SimpleNamespace(side=side, quantity=abs(self.ex))

    async def get_open_orders(self, symbol=None):
        return self.opens

    async def get_all_open_orders(self):
        return self.opens + self.other_opens

    async def get_balance(self):
        return balance(available=self.available)

    async def get_account_balance_snapshot(self):
        out = balance(available=self.available)
        out.total_unrealized_pnl = self.upnl
        return out

    async def get_position_risks(self):
        if self.fail_snapshot:
            raise TimeoutError("positionRisk timeout")
        return [] if self.ex == 0 else [
            {"symbol": "BTCUSDT", "positionAmt": str(self.ex),
             "markPrice": "100", "notional": str(self.ex * 100)}]

    async def book_ticker(self, symbol=None):
        return book()

    async def query_order(self, *, client_order_id, symbol):
        return ManagedOrder(client_order_id=client_order_id, symbol=symbol,
                            state=self.query_state,
                            cum_filled_qty=self.query_fill,
                            error="query timeout" if self.query_state == OrderState.UNKNOWN else "")

    async def place_limit_chase(self, **kwargs):
        self.ex += self.drift_before_order
        await kwargs["before_order"](book())
        cid = kwargs["client_order_base"] + "x"
        kwargs["before_submit"](cid)
        self.calls.append(dict(kwargs, cid=cid))
        state = self.result_state
        fill = kwargs["quantity"] if self.partial is None else self.partial
        if state in (OrderState.FILLED, OrderState.CANCELED) and not self.error:
            self.ex += (1 if kwargs["side"] == "LONG" else -1) * fill
        else:
            fill = 0.0
        return OrderResult(ok=fill > 0, symbol=kwargs["symbol"],
                           client_order_id=cid, order_state=state.value,
                           cum_filled_qty=fill, quantity=fill,
                           error=self.error)


def state(target=0.0):
    entry = None if not target else {"side": 1 if target > 0 else -1,
                                     "qty": abs(target)}
    return {"strategies": {"kdj15": {"entry": entry},
                            "eth15": {"entry": None}}}


class TestSyncNetFailures(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.p_save = patch.object(deploy, "save_state", side_effect=lambda st: self.saved.append(copy.deepcopy(st)))
        self.p_ledger = patch.object(deploy, "record_order")
        self.p_save.start()
        self.p_ledger.start()
        self.addCleanup(self.p_save.stop)
        self.addCleanup(self.p_ledger.stop)

    def sync(self, client, st):
        return run(deploy.sync_net(client, st, True, symbol="BTCUSDT", tag="kdj"))

    def test_step_uses_isolated_loss_for_drawdown_breaker(self):
        c = FakeExchange()
        c.upnl = -250.0
        st = state()
        st["peak"] = 1000.0
        # 跳过行情与外部恢复文件；仍走真实 step 中的账户权益与熔断计算。
        with patch.object(deploy, "consume_resume_requests", return_value=[]), \
             patch.object(deploy, "_fetch_symbol_bars", return_value=None):
            run(deploy.step(c, st, True))
        self.assertTrue(st["halted"])
        self.assertEqual(st["day_start_eq"], 750.0)
        self.assertEqual(c.calls, [])

    def test_reversal_requires_confirmed_reduce_only_leg_then_preflight_open(self):
        c = FakeExchange(ex=0.02)
        st = state(-0.01)
        self.sync(c, st)
        self.assertEqual(len(c.calls), 1)
        self.assertEqual((c.calls[0]["side"], c.calls[0]["quantity"], c.calls[0]["reduce_only"]),
                         ("SHORT", 0.02, True))
        self.assertAlmostEqual(c.ex, 0)
        self.sync(c, st)
        self.assertEqual(len(c.calls), 2)
        self.assertEqual((c.calls[1]["side"], c.calls[1]["quantity"], c.calls[1]["reduce_only"]),
                         ("SHORT", 0.01, False))
        self.assertAlmostEqual(c.ex, -0.01)

    def test_partial_cancel_only_retries_remaining_after_requery_position(self):
        c = FakeExchange()
        c.result_state = OrderState.CANCELED
        c.partial = 0.004
        st = state(0.01)
        self.sync(c, st)
        self.assertAlmostEqual(c.ex, 0.004)
        c.partial = None
        self.sync(c, st)
        self.assertAlmostEqual(c.ex, 0.01)
        self.assertAlmostEqual(c.calls[1]["quantity"], 0.006)
        self.assertNotEqual(c.calls[0]["cid"], c.calls[1]["cid"])
        self.assertIsNone(st["execution_guard"]["pending"])

    def test_disaster_partial_restart_continues_reduce_only_ioc_not_refill(self):
        class CrashExchange(FakeExchange):
            async def mark_price(self, symbol=None):
                return 50.0
        c = CrashExchange(ex=0.02)
        c.result_state = OrderState.CANCELED
        c.partial = 0.01
        st = state(0.02)
        stop = run(deploy.disaster_limit_stop(
            c, st, ex_side=0.02, entry_px=100.0, atr_1h=10.0,
            execute=True, symbol="BTCUSDT"))
        self.assertFalse(stop["flat"])
        self.assertAlmostEqual(c.ex, 0.01)
        self.assertIn("BTCUSDT", st["execution_guard"]["emergency_close"])
        self.assertTrue(c.calls[0]["reduce_only"])
        self.assertTrue(c.calls[0]["force_cross"])
        # 模拟在清理虚拟账本前重启：原目标仍是 0.02，但必须先平剩余。
        restarted = copy.deepcopy(st)
        c.partial = None
        self.sync(c, restarted)
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(c.calls[1]["reduce_only"])
        self.assertTrue(c.calls[1]["force_cross"])
        self.assertAlmostEqual(c.calls[1]["quantity"], 0.01)
        self.assertAlmostEqual(c.ex, 0.0)
        self.assertNotIn("BTCUSDT", restarted["execution_guard"]["emergency_close"])

    def test_margin_reject_stops_expansion_without_stopping_safe_close(self):
        c = FakeExchange()
        c.result_state = OrderState.REJECTED
        c.error = 'Binance 400: {"code":-2019,"msg":"Margin is insufficient."}'
        st = state(0.01)
        self.sync(c, st)
        self.assertEqual(st["execution_guard"]["status"], "close_only")
        self.sync(c, st)
        self.assertEqual(len(c.calls), 1, "-2019 不得按轮询周期无限重试")
        c.ex = 0.02
        st["strategies"]["kdj15"]["entry"] = None
        c.result_state = OrderState.FILLED
        c.error = ""
        self.sync(c, st)
        self.assertTrue(c.calls[-1]["reduce_only"])
        self.assertEqual(c.ex, 0)

    def test_unconfirmed_partial_fill_halts_and_restart_keeps_cid(self):
        c = FakeExchange()
        c.result_state = OrderState.UNKNOWN
        c.error = "cancel timeout"
        st = state(0.01)
        self.sync(c, st)
        pending = st["execution_guard"]["pending"]
        self.assertEqual(st["execution_guard"]["status"], "halted")
        self.assertEqual(pending["client_order_id"], c.calls[0]["cid"])
        restarted = copy.deepcopy(st)
        self.sync(c, restarted)
        self.assertEqual(len(c.calls), 1, "重启后的未知成交绝不可重发")
        c.query_state = OrderState.FILLED
        c.query_fill = 0.01
        c.ex = 0.01
        self.sync(c, restarted)
        self.assertIsNone(restarted["execution_guard"]["pending"])
        self.assertEqual(restarted["execution_guard"]["status"], "halted")
        self.assertEqual(len(c.calls), 1, "查询到终态也不能自动解除人工熄火")

    def test_restart_reconciles_terminal_pending_before_new_attempt(self):
        c = FakeExchange()
        c.query_state = OrderState.CANCELED
        st = state(0.01)
        st["execution_guard"] = {"status": "clear", "reason": "", "pending": {
            "client_order_id": "kdj-old-cid", "symbol": "BTCUSDT", "side": "LONG",
            "quantity": 0.01, "reduce_only": False, "exchange_signed": 0.0}}
        self.sync(c, st)
        self.assertEqual(len(c.calls), 1)
        self.assertIsNone(st["execution_guard"]["pending"])
        self.assertNotEqual(c.calls[0]["cid"], "kdj-old-cid")

    def test_restart_filled_but_position_stale_never_resubmits(self):
        c = FakeExchange()
        c.query_state = OrderState.FILLED
        c.query_fill = 0.01
        st = state(0.01)
        st["execution_guard"] = {"status": "clear", "reason": "", "pending": {
            "client_order_id": "kdj-old-cid", "symbol": "BTCUSDT", "side": "LONG",
            "quantity": 0.01, "reduce_only": False, "exchange_signed": 0.0}}
        self.sync(c, st)
        self.assertEqual(c.calls, [])
        self.assertEqual(st["execution_guard"]["status"], "halted")
        self.assertIsNotNone(st["execution_guard"]["pending"])

    def test_preflight_fails_closed_without_sending_any_order(self):
        c = FakeExchange(available=0)
        st = state(0.01)
        self.sync(c, st)
        self.assertEqual(c.calls, [])
        self.assertEqual(st["execution_guard"]["status"], "close_only")
        c2 = FakeExchange()
        c2.fail_snapshot = True
        st2 = state(0.01)
        self.sync(c2, st2)
        self.assertEqual(c2.calls, [])
        self.assertEqual(st2["execution_guard"]["status"], "close_only")

    def test_position_changes_between_preflight_and_post_no_order(self):
        c = FakeExchange()
        c.drift_before_order = 0.002
        st = state(0.01)
        self.sync(c, st)
        self.assertEqual(c.calls, [])
        self.assertEqual(st["execution_guard"]["status"], "close_only")
        self.assertIsNone(st["execution_guard"]["pending"])

    def test_existing_daily_halt_must_not_refill_expansion(self):
        c = FakeExchange(ex=0.005)
        st = state(0.01)
        run(deploy.sync_net(c, st, True, symbol="BTCUSDT", tag="kdj", allow_expand=False))
        self.assertEqual(c.calls, [])

    def test_open_order_blocks_and_sub_step_residue_never_crosses_zero(self):
        c = FakeExchange(ex=0.0005)
        st = state(-0.01)
        self.sync(c, st)
        self.assertEqual(c.calls, [])
        self.assertEqual(st["execution_guard"]["status"], "halted")
        c2 = FakeExchange(ex=0.01)
        c2.other_opens = [{"symbol": "ETHUSDT", "clientOrderId": "other-open"}]
        st2 = state(0.02)
        self.sync(c2, st2)
        self.assertEqual(c2.calls, [])
        self.assertEqual(st2["execution_guard"]["status"], "close_only")
        c3 = FakeExchange(ex=0.01)
        c3.other_opens = [{"symbol": "ETHUSDT", "clientOrderId": "other-open"}]
        st3 = state(0)
        self.sync(c3, st3)
        self.assertTrue(c3.calls[0]["reduce_only"])
        self.assertEqual(c3.ex, 0)


class TestClientFaults(unittest.TestCase):
    def test_explicit_2019_reject_no_cancel_or_retry(self):
        c = _StubBinance([BinanceClientError('Binance 400: {"code":-2019}')])
        res = run(c.place_limit_order("LONG", 0.01, 100,
                                      fill_timeout_sec=0.01, poll_interval_sec=0.001,
                                      cancel_if_unfilled=True, post_only=True))
        self.assertEqual(res.order_state, OrderState.REJECTED.value)
        self.assertEqual(len(c.submitted), 1)
        self.assertEqual(c.cancelled, [])

    def test_market_path_never_retries_2019_or_auto_flips_4061(self):
        for code in (-2019, -4061):
            with self.subTest(code=code):
                c = _StubBinance([BinanceClientError(f'Binance 400: {{"code":{code}}}')])
                res = run(c.market_open("LONG", 0.01))
                self.assertEqual(res.order_state, OrderState.REJECTED.value)
                self.assertEqual(len(c.submitted), 1)

    def test_cancel_timeout_with_known_partial_cannot_be_called_success(self):
        c = _StubBinance([_raw(status="NEW"),
                          _raw(status="PARTIALLY_FILLED", executed="0.0004"),
                          BinanceClientError("cancel timeout")])
        res = run(c.place_limit_order("LONG", 0.001, 100,
                                      fill_timeout_sec=0.01, poll_interval_sec=0.002,
                                      cancel_if_unfilled=True, post_only=True))
        self.assertFalse(res.ok)
        self.assertEqual(res.order_state, OrderState.UNKNOWN.value)
        self.assertAlmostEqual(res.cum_filled_qty, 0.0004)

    def test_chase_stops_on_unknown_or_margin_reject_before_ioc(self):
        class Bad(_PassiveStub):
            error_state = OrderState.UNKNOWN
            async def place_limit_order(self, *args, **kwargs):
                self.calls.append(kwargs)
                return OrderResult(ok=False, client_order_id=kwargs["client_order_id"],
                                   order_state=self.error_state.value,
                                   error="-2019" if self.error_state == OrderState.REJECTED else "timeout")
        c = Bad()
        res = run(c.place_limit_chase("LONG", 0.01, window_sec=0.01))
        self.assertEqual(res.order_state, OrderState.UNKNOWN.value)
        self.assertEqual(len(c.calls), 1)
        c2 = Bad()
        c2.error_state = OrderState.REJECTED
        res2 = run(c2.place_limit_chase("LONG", 0.01, window_sec=0.01))
        self.assertEqual(res2.order_state, OrderState.REJECTED.value)
        self.assertEqual(len(c2.calls), 1)

    def test_persist_callback_failure_prevents_post(self):
        c = _PassiveStub(fill_at_step=0)
        def fail(cid):
            raise OSError("disk full")
        with self.assertRaisesRegex(OSError, "disk full"):
            run(c.place_limit_chase("LONG", 0.001, before_submit=fail))
        self.assertEqual(c.calls, [])

    def test_ioc_unknown_partial_does_not_claim_success(self):
        class UnknownPartial(_PassiveStub):
            async def place_limit_order(self, *args, **kwargs):
                self.calls.append(kwargs)
                return OrderResult(ok=False, client_order_id=kwargs["client_order_id"],
                                   order_state=OrderState.UNKNOWN.value,
                                   cum_filled_qty=0.001, error="IOC query timeout")
        c = UnknownPartial()
        res = run(c.place_limit_chase("LONG", 0.002, force_cross=True))
        self.assertFalse(res.ok)
        self.assertEqual(res.order_state, OrderState.UNKNOWN.value)
        self.assertEqual(res.cum_filled_qty, 0.001)
        self.assertEqual(len(c.calls), 1)

    def test_real_step_residue_is_not_rounded_away(self):
        c = _PassiveStub(fill_at_step=0)
        self.assertEqual(c._qty_precision(5.260 - 5.259, 0.001), 0.001)
        self.assertEqual(c._qty_precision(0.0009999, 0.001), 0.0)


if __name__ == "__main__":
    unittest.main()
