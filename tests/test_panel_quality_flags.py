"""面板质量卡标志位测试。

2026-10-04：`five_minute_disabled` 原先用 `"5" in strategy_id` 判断，
而 kdj15 / eth15 里也含字符 5 → 5m 明明已停用却报 False（误报「5m 还在跑」）。
现在按 SPEC_5M / SPEC_ETH_5M 的 id 精确判定。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from review import panel_server as ps
from shadow.strategy_books import SPEC_5M, SPEC_ETH_5M


class TestFiveMinuteDisabledFlag(unittest.TestCase):
    def _flag_for(self, heartbeat_strategies):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "runtime", "shadow")
            os.makedirs(base)
            with open(os.path.join(base, "runner_heartbeat.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"strategies": heartbeat_strategies}, fh)
            with open(os.path.join(base, "deployed_state.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"strategies": {s: {} for s in heartbeat_strategies},
                           "halted": False}, fh)
            open(os.path.join(base, "deployed_trades.csv"), "w").close()
            old_root = ps.ROOT
            ps.ROOT = tmp
            try:
                return ps._quality_context(0).get("five_minute_disabled")
            finally:
                ps.ROOT = old_root

    def test_only_15m_running_means_five_minute_is_disabled(self):
        # 回归点：曾因 kdj15/eth15 里含 '5' 而误判成 False
        self.assertTrue(self._flag_for(["kdj15", "eth15"]))

    def test_five_minute_strategy_running_is_not_disabled(self):
        self.assertFalse(self._flag_for([SPEC_5M.id, SPEC_ETH_5M.id]))

    def test_mixed_set_counts_as_running(self):
        self.assertFalse(self._flag_for(["kdj15", SPEC_5M.id]))

    def test_empty_heartbeat_means_disabled(self):
        self.assertTrue(self._flag_for([]))


if __name__ == "__main__":
    unittest.main()
