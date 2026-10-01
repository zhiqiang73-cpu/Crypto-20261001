"""对账恢复与限价单成交状态机测试 — 无网络副作用。

覆盖两处已确认的真实缺陷：
  1. 交易所与本地仓位对账后，陈旧的开仓阻塞无法在运行期解除
  2. 限价单刚提交返回 NEW 被误判为失败并取消，导致遗留仓位
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.binance_client import BinanceClientError, BinanceTestnetClient
from trading.fake_exchange import FakeBinanceClient
from trading.models import HorizonPosition, OrderState
from trading.position_manager import PositionManager


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- stubs
def _raw(
    *,
    order_id: int = 1,
    status: str = "NEW",
    executed: str = "0",
    avg: str = "0",
    qty: str = "0.001",
    side: str = "BUY",
) -> dict:
    return {
        "orderId": order_id,
        "clientOrderId": "stub",
        "symbol": "BTCUSDT",
        "side": side,
        "status": status,
        "origQty": qty,
        "executedQty": executed,
        "avgPrice": avg,
        "type": "LIMIT",
    }


class _StubBinance(BinanceTestnetClient):
    """脚本化交易所响应，用于驱动 place_limit_order 的状态机。

    script 中每个元素对应一次交易所响应；元素为异常实例时抛出该异常。
    脚本耗尽后重复返回最后一个元素，避免测试因轮询次数而脆弱。
    """

    def __init__(self, script, tick: str = "0.10", step: str = "0.001") -> None:
        super().__init__(
            api_key="stub-key",
            api_secret="stub-secret",
            base_url="https://testnet.binancefuture.com",
        )
        self.script = list(script)
        self._tick = tick
        self._step = step
        self.submitted: list = []
        self.cancelled: list = []

    def _next(self):
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0] if self.script else {}

    async def exchange_info(self, force: bool = False) -> dict:
        return {
            "symbols": [
                {
                    "symbol": self.symbol,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": self._tick},
                        {"filterType": "LOT_SIZE", "stepSize": self._step},
                    ],
                }
            ]
        }

    async def get_position_mode(self) -> bool:
        return False

    async def _request(self, method, path, params=None, signed=False, **kw):
        if path not in ("/fapi/v1/order", "/fapi/v1/algoOrder"):
            raise AssertionError(f"unexpected path {method} {path}")
        if method == "POST":
            self.submitted.append(dict(params or {}))
        elif method == "DELETE":
            self.cancelled.append(dict(params or {}))
        step = self._next()
        if isinstance(step, BaseException):
            raise step
        return step


# ------------------------------------------------------- 限价单成交状态机
class TestLimitOrderStateMachine(unittest.TestCase):
    def test_new_then_filled_is_success(self):
        """提交返回 NEW 属正常，轮询后成交必须判定成功，且不得撤单。"""
        client = _StubBinance([_raw(status="NEW"), _raw(status="FILLED", executed="0.001", avg="83000.5")])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.5, poll_interval_sec=0.001
        ))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(res.cum_filled_qty, 0.001)
        self.assertEqual(res.order_state, OrderState.FILLED.value)
        self.assertEqual(client.cancelled, [], "已成交订单不得被撤销")

    def test_new_unfilled_is_not_cancelled_by_default(self):
        """始终 NEW 且零成交：判定未成交，但默认不撤单、不标记 REJECTED。"""
        client = _StubBinance([_raw(status="NEW")])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.05, poll_interval_sec=0.001
        ))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "unfilled_after_timeout")
        self.assertEqual(res.order_state, OrderState.ACKNOWLEDGED.value)
        self.assertEqual(client.cancelled, [])

    def test_partial_fill_is_reported(self):
        """部分成交必须按实际成交量返回，供调用方按真实数量建立保护单。"""
        client = _StubBinance([
            _raw(status="NEW"),
            _raw(status="PARTIALLY_FILLED", executed="0.0005", avg="83000"),
        ])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.5, poll_interval_sec=0.001
        ))
        self.assertTrue(res.ok)
        self.assertAlmostEqual(res.cum_filled_qty, 0.0005)
        self.assertEqual(res.order_state, OrderState.PARTIALLY_FILLED.value)

    def test_network_error_then_actually_filled(self):
        """提交时网络异常但订单实际成交：必须查询后判定成交，不得假设失败。"""
        client = _StubBinance([
            BinanceClientError("connection reset"),
            _raw(status="FILLED", executed="0.001", avg="83001"),
        ])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.5, poll_interval_sec=0.001
        ))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(res.cum_filled_qty, 0.001)
        self.assertEqual(client.cancelled, [])

    def test_cancel_only_when_explicitly_requested(self):
        """只有显式要求才撤单，且撤单后成交量为零。"""
        client = _StubBinance([
            _raw(status="NEW"),
            _raw(status="NEW"),
            _raw(status="CANCELED"),
        ])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0,
            fill_timeout_sec=0.05, poll_interval_sec=0.001,
            cancel_if_unfilled=True,
        ))
        self.assertFalse(res.ok)
        self.assertEqual(len(client.cancelled), 1)
        self.assertEqual(res.order_state, OrderState.CANCELED.value)
        self.assertAlmostEqual(res.cum_filled_qty, 0.0)

    def test_partial_fill_then_canceled_keeps_filled_qty(self):
        """部分成交后被取消：必须保留实际成交量，不能归零。"""
        client = _StubBinance([
            _raw(status="NEW"),
            _raw(status="CANCELED", executed="0.0004", avg="83000"),
        ])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.5, poll_interval_sec=0.001
        ))
        self.assertAlmostEqual(res.cum_filled_qty, 0.0004)

    def test_order_does_not_exist_is_rejected(self):
        """交易所明确表示订单不存在（-2013）才判定为拒单。"""
        client = _StubBinance([
            BinanceClientError('Binance 400 /fapi/v1/order: {"code":-2013,"msg":"Order does not exist."}'),
        ])
        res = _run(client.place_limit_order(
            "LONG", 0.001, 83000.0, fill_timeout_sec=0.05, poll_interval_sec=0.001
        ))
        self.assertFalse(res.ok)
        self.assertEqual(res.order_state, OrderState.REJECTED.value)

    def test_price_is_aligned_to_tick(self):
        """价格必须对齐 tick，避免 -4014 Price not increased by tick size."""
        client = _StubBinance([_raw(status="FILLED", executed="0.001", avg="83000")])
        _run(client.place_limit_order(
            "LONG", 0.001, 83000.06, fill_timeout_sec=0.05, poll_interval_sec=0.001
        ))
        self.assertEqual(client.submitted[0]["price"], 83000.1)

    def test_quantity_is_aligned_to_step(self):
        """数量必须对齐 stepSize。"""
        client = _StubBinance([_raw(status="FILLED", executed="0.001", avg="83000")])
        _run(client.place_limit_order(
            "LONG", 0.0014, 83000.0, fill_timeout_sec=0.05, poll_interval_sec=0.001
        ))
        self.assertAlmostEqual(client.submitted[0]["quantity"], 0.001)

    def test_price_precision_helper(self):
        self.assertAlmostEqual(BinanceTestnetClient._price_precision(None, 83000.06, 0.1), 83000.1)
        self.assertAlmostEqual(BinanceTestnetClient._price_precision(None, 83000.04, 0.1), 83000.0)


# -------------------------------------------------- 保护单触发价精度
class TestStopPriceTickAlignment(unittest.TestCase):
    """保护单触发价必须按 tick 对齐。

    历史缺陷：硬编码 round(..., 2) 会触发 -4014 Price not increased by
    tick size，导致止损单被交易所拒绝而静默失效。
    """

    def _algo_raw(self) -> dict:
        return {
            "algoId": 9,
            "clientAlgoId": "stub",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "algoStatus": "NEW",
            "quantity": "0.001",
        }

    def test_stop_trigger_price_aligned_to_tick(self):
        client = _StubBinance([self._algo_raw()])
        mo = _run(client.place_stop_market("LONG", 0.001, 82999.96))
        self.assertEqual(client.submitted[0]["triggerPrice"], 83000.0)
        # 未对齐的价格必须被吸附到最近的 tick 整数倍
        client2 = _StubBinance([self._algo_raw()])
        _run(client2.place_stop_market("LONG", 0.001, 82999.94))
        self.assertEqual(client2.submitted[0]["triggerPrice"], 82999.9)
        self.assertEqual(mo.state, OrderState.ACKNOWLEDGED)
        self.assertTrue(mo.is_stop)

    def test_stop_trigger_price_aligned_on_finer_tick(self):
        client = _StubBinance([self._algo_raw()], tick="0.01")
        _run(client.place_stop_market("LONG", 0.001, 82999.944))
        self.assertEqual(client.submitted[0]["triggerPrice"], 82999.94)

    def test_stop_network_error_is_unknown_not_rejected(self):
        """网络错误不得把保护单误判为拒单，否则会掩盖真实的保护缺失。"""
        client = _StubBinance([BinanceClientError("connection reset")])
        mo = _run(client.place_stop_market("LONG", 0.001, 83000.0))
        self.assertEqual(mo.state, OrderState.UNKNOWN)

    def test_stop_explicit_reject_is_rejected(self):
        client = _StubBinance([
            BinanceClientError('Binance 400: {"code":-4014,"msg":"Price not increased by tick size."}')
        ])
        mo = _run(client.place_stop_market("LONG", 0.001, 83000.0))
        self.assertEqual(mo.state, OrderState.REJECTED)


# ------------------------------------------------------------ 对账恢复
class TestReconcileNow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "positions.json"
        self.client = FakeBinanceClient(equity=5000.0, mark=100.0)
        self.mgr = PositionManager(self.client, persist_path=self.path)
        self.mgr._ready = True

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_local(self, side: str, qty: float) -> None:
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side=side, quantity=qty,
            entry_price=100.0, original_quantity=qty, peak_price=100.0,
        )

    def test_consistent_clears_stale_block(self):
        """一致时必须清除陈旧阻塞，无需重启。"""
        self.mgr.reconciliation_needed = True
        self.mgr._allow_new_entries = False
        res = _run(self.mgr.reconcile_now())
        self.assertTrue(res["ok"])
        self.assertEqual(res["stage"], "consistent")
        self.assertFalse(self.mgr.reconciliation_needed)
        self.assertTrue(self.mgr._allow_new_entries)

    def test_orphan_exchange_position_keeps_block(self):
        """本地空、交易所有仓：保持阻塞并记录孤立仓数量。"""
        _run(self.client.market_open("LONG", 0.01))
        res = _run(self.mgr.reconcile_now())
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "orphan_exchange_position")
        self.assertTrue(self.mgr.reconciliation_needed)
        self.assertFalse(self.mgr._allow_new_entries)
        self.assertAlmostEqual(self.mgr.orphan_exchange_qty, 0.01)

    def test_external_flat_clears_local_book(self):
        """本地有仓、交易所已空：按外部平仓清空本地账本并解除阻塞。"""
        self._seed_local("LONG", 0.01)
        self.mgr.reconciliation_needed = True
        self.mgr._allow_new_entries = False
        res = _run(self.mgr.reconcile_now())
        self.assertTrue(res["ok"])
        self.assertEqual(res["stage"], "external_flat_applied")
        self.assertIsNone(self.mgr.positions["short_term"])
        self.assertFalse(self.mgr.reconciliation_needed)
        self.assertTrue(self.mgr._allow_new_entries)

    def test_quantity_mismatch_keeps_block(self):
        """两边都有仓但数量不符：不得伪造归属，保持阻塞。"""
        self._seed_local("LONG", 0.01)
        _run(self.client.market_open("LONG", 0.02))
        res = _run(self.mgr.reconcile_now())
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "quantity_mismatch")
        self.assertTrue(self.mgr.reconciliation_needed)
        self.assertFalse(self.mgr._allow_new_entries)

    def test_exchange_error_keeps_block(self):
        """交易所查询失败：保持阻塞，不假装成功。"""

        class _Boom:
            async def get_position(self, symbol=None):
                raise RuntimeError("network down")

        self.mgr.client = _Boom()
        res = _run(self.mgr.reconcile_now())
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "get_position")
        self.assertTrue(self.mgr.reconciliation_needed)
        self.assertFalse(self.mgr._allow_new_entries)

    def test_short_side_mismatch_keeps_block(self):
        """方向相反也必须视为不一致。"""
        self._seed_local("LONG", 0.01)
        _run(self.client.market_open("SHORT", 0.01))
        res = _run(self.mgr.reconcile_now())
        self.assertFalse(res["ok"])
        self.assertTrue(self.mgr.reconciliation_needed)

    def test_snapshot_exposes_recovery_fields(self):
        """快照必须暴露前端恢复所需的字段。"""
        snap = self.mgr.reconciliation_snapshot()
        for key in ("reconciliation_needed", "allow_new_entries", "local_net",
                    "orphan_exchange_qty", "positions", "state_version"):
            self.assertIn(key, snap)


if __name__ == "__main__":
    unittest.main()
