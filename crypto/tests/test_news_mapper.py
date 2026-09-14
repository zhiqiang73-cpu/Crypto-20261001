"""消息面 mapper 别名 — 与 test_prediction_mapper 拆分便于发现."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mappers.news_mapper import NewsFactorMapper
from models.signals import StrategyHorizon
from models.snapshots import NewsSnapshot


class TestNewsMapperStandalone(unittest.TestCase):
    def test_all_missing_still_scores_halving(self):
        snap = NewsSnapshot(
            months_since_halving=8,
            months_to_halving=20,
            available=True,
            missing_fields=["etf_flows", "monetary_policy"],
        )
        r = NewsFactorMapper().map(snap, StrategyHorizon.LONG_TERM)
        self.assertNotEqual(r.sub_scores["halving"], 0)
        self.assertIn("etf_flows", r.missing_fields)


if __name__ == "__main__":
    unittest.main()
