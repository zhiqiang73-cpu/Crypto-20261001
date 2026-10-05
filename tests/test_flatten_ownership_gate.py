"""面板平仓的归属闸门测试。

2026-10-04：面板的 legacy executor 只认自己的账本，看不到 shadow runner 的仓位，
于是把策略仓当「孤儿仓」；面板上点「清孤儿仓 / 立即平仓」会真的把策略仓平掉。
新增的闸门必须在**证据充分时拒绝**，且**不误伤**方向相反或数量不符的仓位。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from review import panel_server as ps
from shadow.strategy_books import SPECS, empty_book


class TestShadowLedgerClaim(unittest.TestCase):
    def _with_ledger(self, nets):
        """把 ROOT 指到含合成 runner 账本的临时目录。

        nets 是 {symbol: 带方向净仓}，写进真实的 strategies.<spec>.entry 形状
        —— shadow_ledger_claim 经 desired_nets() 读的是这个结构。
        """
        st = {"strategies": {spec.id: empty_book() for spec in SPECS}}
        for symbol, qty in nets.items():
            if abs(float(qty)) < 1e-9:
                continue
            spec = next(s for s in SPECS if s.symbol == symbol)
            st["strategies"][spec.id]["entry"] = {
                "side": 1 if qty > 0 else -1, "qty": abs(float(qty)),
            }
        tmp = tempfile.TemporaryDirectory()
        base = os.path.join(tmp.name, "runtime", "shadow")
        os.makedirs(base)
        with open(os.path.join(base, "deployed_state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(st, fh)
        old_root = ps.ROOT
        ps.ROOT = tmp.name
        self.addCleanup(lambda: (setattr(ps, "ROOT", old_root), tmp.cleanup()))
        return tmp

    def test_matching_net_is_claimed_by_shadow(self):
        self._with_ledger({"BTCUSDT": 0.181})
        self.assertTrue(ps.shadow_ledger_claim("BTCUSDT", 0.1809))

    def test_lot_rounding_within_deadband_still_claimed(self):
        """步长取整差一个步长（0.001）仍算同一笔仓。"""
        self._with_ledger({"BTCUSDT": 0.181})
        self.assertTrue(ps.shadow_ledger_claim("BTCUSDT", 0.180))

    def test_opposite_direction_is_not_claimed(self):
        self._with_ledger({"BTCUSDT": 0.181})
        self.assertEqual(ps.shadow_ledger_claim("BTCUSDT", -0.1809), "")

    def test_large_size_mismatch_is_not_claimed(self):
        self._with_ledger({"BTCUSDT": 0.181})
        self.assertEqual(ps.shadow_ledger_claim("BTCUSDT", 5.0), "")

    def test_flat_exchange_is_never_claimed(self):
        self._with_ledger({"BTCUSDT": 0.181})
        self.assertEqual(ps.shadow_ledger_claim("BTCUSDT", 0.0), "")

    def test_flat_ledger_never_blocks(self):
        """runner 自己空仓时，面板处理别的仓不该被拦。"""
        self._with_ledger({"BTCUSDT": 0.0})
        self.assertEqual(ps.shadow_ledger_claim("BTCUSDT", 0.5), "")

    def test_short_ledger_claims_short_exchange(self):
        self._with_ledger({"ETHUSDT": -9.567})
        self.assertTrue(ps.shadow_ledger_claim("ETHUSDT", -9.566))

    def test_unreadable_state_does_not_block(self):
        """读不到 runner 状态时保守放行，避免把面板自己的仓也锁死。"""
        tmp = tempfile.TemporaryDirectory()
        old_root = ps.ROOT
        ps.ROOT = tmp.name
        try:
            self.assertEqual(ps.shadow_ledger_claim("BTCUSDT", 0.5), "")
        finally:
            ps.ROOT = old_root
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
