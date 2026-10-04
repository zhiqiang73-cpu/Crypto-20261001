"""风险预算、往返成本与净保本价的口径测试。

这些测试守的是**算术口径**，不涉及交易所。口径错了，后面的保护单做得再对
也是错的 —— 所以这里逐项断言公式，而不是只看数量级。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.cost_model import (
    MAKER_FEE_RATE,
    RISK_PER_SYMBOL_RATIO,
    RISK_PORTFOLIO_RATIO,
    TAKER_FEE_RATE,
    layer_allowed,
    net_break_even_price,
    plan_quantity,
    planned_loss,
    portfolio_budget,
    risk_per_unit,
    risk_per_unit_existing,
    stop_is_tighter,
    stop_price_from_avg,
    symbol_budget,
)


class TestFeeConstants(unittest.TestCase):
    def test_fee_rates_match_measured(self):
        """费率必须与 2026-10-04 真实成交反算结果一致。"""
        self.assertAlmostEqual(MAKER_FEE_RATE, 0.0002, places=10)
        self.assertAlmostEqual(TAKER_FEE_RATE, 0.0004, places=10)

    def test_risk_budget_ratios(self):
        self.assertAlmostEqual(RISK_PER_SYMBOL_RATIO, 0.005, places=10)
        self.assertAlmostEqual(RISK_PORTFOLIO_RATIO, 0.01, places=10)

    def test_budgets_at_5000_equity(self):
        """需求原文：约 5,000 USDT 权益时，单标的约 25、组合约 50。"""
        self.assertAlmostEqual(symbol_budget(5000.0), 25.0, places=6)
        self.assertAlmostEqual(portfolio_budget(5000.0), 50.0, places=6)


class TestRiskPerUnit(unittest.TestCase):
    def test_breakdown_components(self):
        rb = risk_per_unit(entry_price=85000.0, stop_price=84810.73,
                           entry_fee_rate=TAKER_FEE_RATE,
                           exit_fee_rate=TAKER_FEE_RATE,
                           slippage_rate=0.0005, funding_rate=0.0002)
        self.assertAlmostEqual(rb.stop_distance, 189.27, places=6)
        self.assertAlmostEqual(rb.entry_fee, 85000.0 * 0.0004, places=6)
        self.assertAlmostEqual(rb.exit_fee, 84810.73 * 0.0004, places=6)
        self.assertAlmostEqual(rb.slippage, 84810.73 * 0.0005, places=6)
        self.assertAlmostEqual(rb.funding, 85000.0 * 0.0002, places=6)
        total = (189.27 + 34.0 + 33.924292 + 42.405365 + 17.0)
        self.assertAlmostEqual(rb.total, total, places=4)

    def test_stop_distance_is_symmetric(self):
        """多空的风险拆解必须对称 —— 止损距离取绝对值。"""
        up = risk_per_unit(entry_price=100.0, stop_price=98.0)
        down = risk_per_unit(entry_price=100.0, stop_price=102.0)
        self.assertAlmostEqual(up.stop_distance, down.stop_distance, places=9)

    def test_zero_inputs_return_zero(self):
        rb = risk_per_unit(entry_price=0.0, stop_price=100.0)
        self.assertEqual(rb.total, 0.0)


class TestRiskPerUnitExisting(unittest.TestCase):
    def test_uses_actual_paid_fees(self):
        """已有仓位必须用真实已付费用，而不是费率假设。"""
        rb = risk_per_unit_existing(
            avg_price=85042.07, stop_price=84852.8, quantity=0.296,
            entry_fee_paid=6.5969, funding_paid=2.45,
        )
        self.assertAlmostEqual(rb.entry_fee, 6.5969 / 0.296, places=8)
        self.assertAlmostEqual(rb.funding, 2.45 / 0.296, places=8)
        self.assertAlmostEqual(rb.stop_distance, 85042.07 - 84852.8, places=8)

    def test_zero_quantity_returns_zero(self):
        rb = risk_per_unit_existing(avg_price=100.0, stop_price=98.0,
                                    quantity=0.0, entry_fee_paid=1.0,
                                    funding_paid=0.0)
        self.assertEqual(rb.total, 0.0)


class TestPlanQuantity(unittest.TestCase):
    def test_btc_example_5000_equity(self):
        """示例一：5,000 权益、BTC 85,000、1.5×ATR_1H=189.27 的止损。"""
        plan = plan_quantity(
            equity=5000.0, entry_price=85000.0, stop_price=84810.73,
            step=0.001, min_notional=100.0,
        )
        self.assertTrue(plan.ok)
        # 每单位风险 ≈ 316.6，25 / 316.6 ≈ 0.07896 → 向下取整到 0.078
        self.assertAlmostEqual(plan.quantity, 0.078, places=9)
        self.assertLessEqual(plan.planned_loss, 25.0 + 1e-9,
                             "计划亏损不得超过风险预算")
        self.assertAlmostEqual(plan.risk_budget, 25.0, places=6)

    def test_rounds_down_never_up(self):
        """必须向下取整 —— 向上取整会直接突破风险预算。"""
        plan = plan_quantity(equity=5000.0, entry_price=100.0,
                             stop_price=99.0, step=1.0)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.quantity % 1.0, 0.0)
        self.assertLessEqual(plan.planned_loss, 25.0 + 1e-9)

    def test_does_not_tighten_stop_to_fit(self):
        """预算不够时跳过，不得靠拉近止损硬凑仓位。"""
        plan = plan_quantity(equity=10.0, entry_price=85000.0,
                             stop_price=84810.73, step=0.001)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.quantity, 0.0)
        self.assertIn("步长", plan.reason)

    def test_below_min_notional_is_skipped(self):
        plan = plan_quantity(equity=5000.0, entry_price=85000.0,
                             stop_price=84810.73, step=0.001,
                             min_notional=100000.0)
        self.assertFalse(plan.ok)
        self.assertIn("最小下单额", plan.reason)

    def test_zero_stop_distance_rejected(self):
        plan = plan_quantity(equity=5000.0, entry_price=100.0,
                             stop_price=100.0, step=0.001)
        self.assertFalse(plan.ok)
        self.assertIn("止损距离为 0", plan.reason)

    def test_explicit_risk_budget_overrides_ratio(self):
        plan = plan_quantity(equity=5000.0, entry_price=100.0,
                             stop_price=99.0, step=1.0, risk_budget=7.0)
        self.assertTrue(plan.ok)
        self.assertAlmostEqual(plan.risk_budget, 7.0, places=9)


class TestLayerGate(unittest.TestCase):
    def test_layer_allowed_within_budget(self):
        ok, why = layer_allowed(equity=5000.0, symbol_loss_after=20.0,
                                portfolio_loss_after=40.0)
        self.assertTrue(ok, why)

    def test_layer_rejected_when_symbol_over(self):
        ok, why = layer_allowed(equity=5000.0, symbol_loss_after=26.0,
                                portfolio_loss_after=40.0)
        self.assertFalse(ok)
        self.assertIn("单标的风险", why)

    def test_layer_rejected_when_portfolio_over(self):
        ok, why = layer_allowed(equity=5000.0, symbol_loss_after=20.0,
                                portfolio_loss_after=51.0)
        self.assertFalse(ok)
        self.assertIn("组合风险", why)


class TestPlannedLoss(unittest.TestCase):
    def test_includes_paid_costs(self):
        loss = planned_loss(quantity=0.296, entry_price=85042.07,
                            stop_price=84852.8, entry_fee_paid=6.5969,
                            funding_paid=2.45)
        # 价格损失 + 已付手续费 + 已付资金费 + 退出费 + 滑点
        self.assertGreater(loss, 0.296 * (85042.07 - 84852.8))
        self.assertGreater(loss, 6.5969 + 2.45)

    def test_zero_quantity(self):
        self.assertEqual(planned_loss(quantity=0.0, entry_price=100.0,
                                      stop_price=99.0), 0.0)


class TestNetBreakEven(unittest.TestCase):
    def test_long_above_avg(self):
        px, why = net_break_even_price(
            avg_price=85042.07, side=1, quantity=0.296,
            entry_fee_paid=6.5969, funding_paid=2.45, tick=0.10,
        )
        self.assertGreater(px, 85042.07, "多仓净保本价必须在均价之上")
        # 固定成本 = (入场费 6.5969 + 资金费 2.45) / 0.296 = 30.564 / 单位
        # 比例成本 = 退出手续费 4bp + 滑点预算 5bp = 9bp，按保本价本身计，
        # 所以是解方程而不是简单相加：
        #     P = (avg + fixed) / (1 − 9bp) ≈ 85,149.3，比均价高约 107
        fixed = (6.5969 + 2.45) / 0.296
        expected = (85042.07 + fixed) / (1.0 - 0.0004 - 0.0005)
        import math
        expected = math.ceil(round(expected / 0.10, 9)) * 0.10
        self.assertAlmostEqual(px, expected, places=6)
        self.assertAlmostEqual(px - 85042.07, 107.23, delta=0.11)

    def test_short_below_avg(self):
        px, why = net_break_even_price(
            avg_price=2701.54, side=-1, quantity=3.707,
            entry_fee_paid=15.45, funding_paid=2.45, tick=0.01,
        )
        self.assertLess(px, 2701.54, "空仓净保本价必须在均价之下")

    def test_rounded_toward_protection(self):
        """多仓向上取整到 tick，宁可多留缓冲。"""
        px, _ = net_break_even_price(
            avg_price=100.0, side=1, quantity=1.0,
            entry_fee_paid=0.0, funding_paid=0.0, tick=0.3,
        )
        n = round(px / 0.3, 9)
        self.assertAlmostEqual(n, round(n), places=6, msg=f"{px} 未对齐 tick")

    def test_missing_entry_fee_uses_conservative_fallback(self):
        px_given, _ = net_break_even_price(
            avg_price=100.0, side=1, quantity=1.0,
            entry_fee_paid=0.04, funding_paid=0.0, tick=0.001,
        )
        px_fallback, why = net_break_even_price(
            avg_price=100.0, side=1, quantity=1.0,
            entry_fee_paid=0.0, funding_paid=0.0, tick=0.001,
            entry_fee_missing=True,
        )
        self.assertIn("保守估算", why)
        self.assertGreater(px_fallback, 100.0)

    def test_zero_quantity_rejected(self):
        px, why = net_break_even_price(avg_price=100.0, side=1, quantity=0.0,
                                       entry_fee_paid=0.0, funding_paid=0.0,
                                       tick=0.1)
        self.assertEqual(px, 0.0)


class TestStopPriceFromAvg(unittest.TestCase):
    def test_long_stop_below_avg(self):
        px = stop_price_from_avg(avg_price=85042.07, side=1, atr_1h=126.18,
                                 multiple=1.5, tick=0.10)
        self.assertAlmostEqual(px, 85042.07 - 189.27, places=1)
        self.assertLess(px, 85042.07)

    def test_short_stop_above_avg(self):
        px = stop_price_from_avg(avg_price=2701.54, side=-1, atr_1h=30.0,
                                 multiple=1.5, tick=0.01)
        self.assertGreater(px, 2701.54)

    def test_tick_aligned(self):
        px = stop_price_from_avg(avg_price=85042.07, side=1, atr_1h=126.18,
                                 multiple=1.5, tick=0.10)
        n = round(px / 0.10, 9)
        self.assertAlmostEqual(n, round(n), places=6)

    def test_bad_inputs(self):
        self.assertEqual(stop_price_from_avg(avg_price=0.0, side=1,
                                             atr_1h=100.0, multiple=1.5,
                                             tick=0.1), 0.0)
        self.assertEqual(stop_price_from_avg(avg_price=100.0, side=1,
                                             atr_1h=0.0, multiple=1.5,
                                             tick=0.1), 0.0)


class TestStopIsTighter(unittest.TestCase):
    def test_long_only_up(self):
        self.assertTrue(stop_is_tighter(new_stop=101.0, old_stop=100.0, side=1))
        self.assertFalse(stop_is_tighter(new_stop=99.0, old_stop=100.0, side=1))
        self.assertFalse(stop_is_tighter(new_stop=100.0, old_stop=100.0, side=1))

    def test_short_only_down(self):
        self.assertTrue(stop_is_tighter(new_stop=99.0, old_stop=100.0, side=-1))
        self.assertFalse(stop_is_tighter(new_stop=101.0, old_stop=100.0, side=-1))

    def test_no_old_stop_is_always_an_improvement(self):
        self.assertTrue(stop_is_tighter(new_stop=99.0, old_stop=0.0, side=1))


if __name__ == "__main__":
    unittest.main()
