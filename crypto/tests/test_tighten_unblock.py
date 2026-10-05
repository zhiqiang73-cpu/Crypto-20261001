"""保护单收紧卡死的三处修复 —— 回归测试。

2026-10-05 实测事故
------------------
日志被 `保护单收紧未确认` 刷屏 1224 次，BTC/ETH 两个标的永久禁止开新仓。
根因是三个缺陷叠加：

  缺陷 1  币安对 `closePosition=true` 的条件单每标的每方向只允许一张，
          旧单还在时新单被 **400 -4130** 明确拒绝 —— 「先立后破」不可行。
  缺陷 2  `-4130` 不在拒单码白名单里，被降级成 UNKNOWN。
  缺陷 3  UNKNOWN 路径丢弃 `mo.error`，只留「查不到」的通用文案 ——
          实测日志里 `-4130` 出现 0 次，诊断方向被误导。

本文件守的就是这三处，外加两处配套（覆盖度检查、失败退避）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

WT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WT))
os.chdir(WT)

from trading.binance_client import BinanceTestnetClient, BinanceClientError  # noqa: E402
from trading.models import ManagedOrder, OrderState  # noqa: E402
from trading import protective_orders as po  # noqa: E402

REJECT_TEXT = ('Binance 400 /fapi/v1/algoOrder: {"code":-4130,'
               '"msg":"An open stop or take profit order with GTE and '
               'closePosition in the direction is existing."}')


# --------------------------------------------------------------------------
# 缺陷 2：-4130 必须归为 REJECTED，不能降级成 UNKNOWN
# --------------------------------------------------------------------------
class TestRejectCodeMapping(unittest.IsolatedAsyncioTestCase):
    async def test_4130_is_classified_as_rejected(self):
        """-4130 是**明确拒单**，必须是 REJECTED 而不是 UNKNOWN。

        UNKNOWN 的语义是「可能已落地，需回查确认」；把明确拒单标成 UNKNOWN
        会让调用方去回查一个根本不存在的单，最后报「查不到」而丢掉真因。
        """
        c = BinanceTestnetClient.__new__(BinanceTestnetClient)
        c.symbol = "BTCUSDT"
        c._hedge_mode = False

        async def fake_tick(sym):
            return 0.1

        async def fake_lot(sym):
            return 0.001

        async def boom(*a, **k):
            raise BinanceClientError(REJECT_TEXT)

        c.price_tick = fake_tick            # type: ignore[assignment]
        c._lot_step = fake_lot              # type: ignore[assignment]
        c._request = boom                   # type: ignore[assignment]

        mo = await c.place_stop_market("LONG", 0.001, 85000.0, "BTCUSDT",
                                       close_position=False)
        self.assertEqual(mo.state, OrderState.REJECTED,
                         "-4130 必须映射为 REJECTED")
        self.assertIn("-4130", str(mo.error))

    async def test_network_error_stays_unknown(self):
        """网络类错误必须仍是 UNKNOWN —— 不能因为修 -4130 而误伤这条语义。"""
        c = BinanceTestnetClient.__new__(BinanceTestnetClient)
        c.symbol = "BTCUSDT"
        c._hedge_mode = False

        async def fake_tick(sym):
            return 0.1

        async def fake_lot(sym):
            return 0.001

        async def boom(*a, **k):
            raise BinanceClientError("Connection reset by peer")

        c.price_tick = fake_tick            # type: ignore[assignment]
        c._lot_step = fake_lot              # type: ignore[assignment]
        c._request = boom                   # type: ignore[assignment]

        mo = await c.place_stop_market("LONG", 0.001, 85000.0, "BTCUSDT",
                                       close_position=False)
        self.assertEqual(mo.state, OrderState.UNKNOWN)


# --------------------------------------------------------------------------
# 缺陷 3：UNKNOWN 路径必须保留交易所原始错误
# --------------------------------------------------------------------------
class _FakeClient:
    """下单返回 UNKNOWN 且带错误文本；openAlgoOrders 永远为空。"""

    def __init__(self, mo_error: str):
        self.mo_error = mo_error
        self.placed: list[dict] = []

    async def place_stop_market(self, side, quantity, stop_price, symbol,
                                *, client_order_id=None, close_position=False,
                                order_type="STOP_MARKET"):
        self.placed.append({"side": side, "quantity": quantity,
                            "close_position": close_position})
        return ManagedOrder(client_order_id=client_order_id or "cid",
                            state=OrderState.UNKNOWN, error=self.mo_error,
                            symbol=symbol, is_stop=True, is_algo=True)

    async def get_open_algo_orders(self, symbol=None):
        return []


class TestErrorNotSwallowed(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_error_survives_unknown_path(self):
        """UNKNOWN 路径必须把交易所返回的原文带进 error。

        修复前：error 只剩「下单后 openAlgoOrders 中查不到该保护单」，
        -4130 整个消失 —— 这正是本 bug 潜伏一整天的原因。
        """
        c = _FakeClient(REJECT_TEXT)
        out = await po.place_protective_stop(
            c, symbol="BTCUSDT", side=1, trigger_price=85000.0,
            quantity=0.001, close_position=False)
        self.assertFalse(out.protects)
        self.assertIn("-4130", out.error,
                      "交易所原始错误必须保留，不能被通用文案覆盖")
        self.assertIn("查不到", out.error, "通用说明也应保留")


# --------------------------------------------------------------------------
# 缺陷 1 的配套：下单必须用显式数量（closePosition=false）
# --------------------------------------------------------------------------
class TestExplicitQuantityPlaced(unittest.IsolatedAsyncioTestCase):
    async def test_tighten_places_explicit_quantity(self):
        """收紧必须先立后破 —— 因此新单必须是显式数量，不能是 closePosition。

        closePosition=true 会被交易所 -4130 拒绝，先立后破无从成立。
        """
        existing = {"algoId": "old-1", "clientAlgoId": "c-old",
                    "orderType": "STOP_MARKET", "side": "SELL",
                    "algoStatus": "NEW", "triggerPrice": "84000.0",
                    "closePosition": "true", "quantity": "0.0"}

        class C(_FakeClient):
            def __init__(self):
                super().__init__("")
                self.canceled: list[str] = []

            async def get_open_algo_orders(self, symbol=None):
                # 新单下完后要能查到（按 clientAlgoId 命中）
                if self.placed:
                    return [{"algoId": "new-1", "clientAlgoId": "c-new",
                             "orderType": "STOP_MARKET", "side": "SELL",
                             "algoStatus": "NEW",
                             "triggerPrice": "85000.0",
                             "closePosition": "false",
                             "quantity": "0.001"}]
                return [existing]

            async def place_stop_market(self, *a, **k):
                mo = await super().place_stop_market(*a, **k)
                mo.state = OrderState.ACKNOWLEDGED
                mo.algo_id = "new-1"
                mo.client_order_id = "c-new"
                return mo

            async def cancel_algo_order(self, *, client_algo_id=None,
                                        algo_id=None, symbol=None):
                self.canceled.append(str(algo_id or client_algo_id))
                return ManagedOrder(client_order_id="", state=OrderState.CANCELED,
                                    symbol=symbol or "", is_algo=True)

        c = C()
        out = await po.tighten_protective_stop(
            c, symbol="BTCUSDT", side=1, new_trigger=85000.0,
            old_algo_id="old-1", quantity=0.001, close_position=False)
        self.assertTrue(out.protects, f"应先立成功: {out.error}")
        self.assertEqual(c.placed[0]["close_position"], False,
                         "新单必须是显式数量（close_position=False）")
        self.assertEqual(c.placed[0]["quantity"], 0.001)
        self.assertIn("old-1", c.canceled, "新单确认后才撤旧单（先立后破）")


# --------------------------------------------------------------------------
# 配套：覆盖度检查 —— 数量不足必须重挂
# --------------------------------------------------------------------------
class TestCoverageCheck(unittest.TestCase):
    def test_undersized_stop_does_not_cover(self):
        o = {"orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW",
             "triggerPrice": "85000.0", "closePosition": "false",
             "quantity": "0.050"}
        self.assertFalse(po.covers_full_position(o, quantity=0.187),
                         "数量不足必须判为未覆盖 —— 否则仓位裸奔")

    def test_oversized_stop_still_covers(self):
        """分批止盈后仓位变小、止损数量偏大：仍算覆盖（reduceOnly 兜底）。"""
        o = {"orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW",
             "triggerPrice": "85000.0", "closePosition": "false",
             "quantity": "0.187"}
        self.assertTrue(po.covers_full_position(o, quantity=0.0468))

    def test_close_position_always_covers(self):
        o = {"orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW",
             "triggerPrice": "85000.0", "closePosition": "true",
             "quantity": "0.0"}
        self.assertTrue(po.covers_full_position(o, quantity=999.0))


# --------------------------------------------------------------------------
# 源码级守门：确保改动不会被静默回退
# --------------------------------------------------------------------------
class TestSourceGuards(unittest.TestCase):
    def test_deploy_uses_explicit_quantity(self):
        src = (WT / "shadow/deploy.py").read_text(encoding="utf-8")
        body = src.split("async def manage_exchange_stop")[1]
        self.assertNotIn("close_position=True", body,
                         "manage_exchange_stop 不得再用 closePosition —— "
                         "会被交易所 -4130 拒绝，先立后破无从成立")
        self.assertEqual(body.count("close_position=False"), 2,
                         "收紧与建立两处都必须是显式数量")

    def test_deploy_has_coverage_check(self):
        src = (WT / "shadow/deploy.py").read_text(encoding="utf-8")
        self.assertIn("covers_full_position(existing", src,
                      "必须有覆盖度检查，否则加仓后止损数量不足、仓位裸奔")

    def test_reverse_uses_net_notional(self):
        """反手必须按**净增名义**算保证金，不能把自己旧仓也算进去。

        2026-10-05 实测：BTC 反手（平 0.1710 空、开 0.1700 多）被算成
        「需 2908.71 > 可用 2482.26」，反手永远做不成 —— 系统用自己的旧仓
        把自己挡住了。
        """
        src = (WT / "shadow/deploy.py").read_text(encoding="utf-8")
        self.assertIn("released = abs(ex) * mark / float(LEVERAGE)", src,
                      "反手必须减掉即将释放的旧仓保证金")
        self.assertIn('cur_side and side.upper() != cur_side', src,
                      "必须只在反向时启用净额口径")

    def test_backoff_counter_exists(self):
        src = (WT / "shadow/deploy.py").read_text(encoding="utf-8")
        self.assertIn("unconfirmed_streak", src,
                      "必须有连续失败计数，否则每 tick 刷屏")
        self.assertIn("streak % 20", src, "打印必须退避")


if __name__ == "__main__":
    unittest.main(verbosity=2)
