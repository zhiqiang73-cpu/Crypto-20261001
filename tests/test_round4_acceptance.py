"""Round-4 工程一致性验收：配置单一真相、巨鲸字段、成交前复核、清算权重。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.scorer import FactorScoringEngine
from models.signals import ActionDecision, DimensionScores, StrategyHorizon
from trading.pretrade import PretradeLimits, recheck_entry
from utils.scoring import detect_collinear_whale


class TestP1ConfigSingleSource(unittest.TestCase):
    def test_boundary_uses_frozen_thresholds_not_module(self):
        eng = FactorScoringEngine(use_active_overrides=False)
        # 工厂 ±20
        self.assertEqual(eng._decide(20).value, "STANDARD_LONG")
        self.assertEqual(eng._decide(19).value, "WATCH_LONG")
        self.assertEqual(eng._decide(-20).value, "STANDARD_SHORT")
        # 注入 ACTIVE 风格 ±35 快照
        th = dict(eng.th)
        th["standard_long"] = 35
        th["standard_short"] = -40
        th["strong_long"] = 60
        th["strong_short"] = -65
        th["watch_long"] = 10
        dim = DimensionScores(news=40, data=40, tech=40, prediction=40)
        # CS≈40 → 工厂 STANDARD，注入后若阈值 35 仍 STANDARD；用 34 测边界
        ev = eng.evaluate(
            StrategyHorizon.SHORT_TERM,
            DimensionScores(news=34, data=34, tech=34, prediction=34),
            thresholds=th,
            weights={"news": 0.25, "data": 0.25, "tech": 0.25, "prediction": 0.25},
        )
        # 34 < 35 → WATCH
        self.assertEqual(ev.decision, ActionDecision.WATCH_LONG)
        ev2 = eng.evaluate(
            StrategyHorizon.SHORT_TERM,
            DimensionScores(news=36, data=36, tech=36, prediction=36),
            thresholds=th,
            weights={"news": 0.25, "data": 0.25, "tech": 0.25, "prediction": 0.25},
        )
        self.assertEqual(ev2.decision, ActionDecision.STANDARD_LONG)

    def test_panel_payload_thresholds_from_snapshot(self):
        from runtime.live_loop import LiveScoreSnapshot
        from runtime.panel_server import snapshot_to_panel
        from models.signals import ActionDecision

        snap = LiveScoreSnapshot(
            timestamp_ms=1,
            mark_price=100.0,
            s_data=10, s_tech=10, s_news=10, s_prediction=10,
            partial_cs=10, composite_score=10,
            decision=ActionDecision.WATCH_LONG,
            is_full_cs=True,
            config_snapshot={
                "version": "v_test",
                "content_hash": "abc",
                "dimension_weights": {
                    "short_term": {"news": 0.15, "data": 0.45, "tech": 0.25, "prediction": 0.15}
                },
                "decision_thresholds": {
                    "strong_long": 60, "standard_long": 35, "watch_long": 10,
                    "neutral_upper": 8, "neutral_lower": -8,
                    "watch_short": -8, "standard_short": -40, "strong_short": -65,
                },
            },
        )
        payload = snapshot_to_panel(snap, StrategyHorizon.SHORT_TERM)
        self.assertEqual(payload["thresholds"]["standard_long"], 35)
        self.assertEqual(payload["config_version"], "v_test")
        self.assertEqual(payload["base_weights"]["data"], 0.45)


class TestP3WhaleField(unittest.TestCase):
    def test_detect_uses_sub_scores_keys(self):
        faces = {"news": 30.0, "data": 25.0, "tech": 0.0, "prediction": 0.0}
        parts = {"news_whale": 40.0, "data_whale": 35.0}
        out = detect_collinear_whale(faces, parts)
        self.assertTrue(out.get("_collinear_whale"))

    def test_wrong_field_would_miss(self):
        # 模拟旧 bug：读 indicator_scores 空 → 不去重
        faces = {"news": 30.0, "data": 25.0, "tech": 0.0, "prediction": 0.0}
        empty = {}
        out = detect_collinear_whale(faces, empty)
        self.assertFalse(bool(out.get("_collinear_whale")))


class TestP3HeatmapNoDirectional(unittest.TestCase):
    def test_heatmap_weight_zero_and_mapper_zero(self):
        from config.weights import DERIVATIVES_INDICATOR_WEIGHTS
        self.assertEqual(DERIVATIVES_INDICATOR_WEIGHTS["short_term"]["liquidation_heatmap"], 0.0)
        from mappers.data_mapper import DataFactorMapper
        m = DataFactorMapper()
        missing = []
        self.assertEqual(m._map_heatmap(0.8, missing), 0.0)


class TestP4Pretrade(unittest.TestCase):
    def test_adverse_05_atr_blocks(self):
        r = recheck_entry(
            side="LONG",
            signal_price=100.0,
            signal_ts_ms=1000,
            now_ms=2000,
            exec_mark=100.6,
            quote_ts_ms=2000,
            atr=1.0,
            hard_sl_distance=1.0,
            tp_distance=1.2,
            limits=PretradeLimits(max_adverse_atr=0.5),
        )
        self.assertFalse(r.ok)
        self.assertIn("adverse_move_atr", r.reasons)

    def test_fresh_ok(self):
        r = recheck_entry(
            side="LONG",
            signal_price=100.0,
            signal_ts_ms=1000,
            now_ms=1500,
            exec_mark=100.1,
            quote_ts_ms=1500,
            atr=1.0,
            hard_sl_distance=1.0,
            tp_distance=1.2,
            limits=PretradeLimits(max_adverse_atr=0.5),
        )
        self.assertTrue(r.ok)

    def test_signal_expired(self):
        r = recheck_entry(
            side="SHORT",
            signal_price=100.0,
            signal_ts_ms=0,
            now_ms=200_000,
            exec_mark=100.0,
            quote_ts_ms=200_000,
            atr=1.0,
            limits=PretradeLimits(signal_ttl_sec=120),
        )
        self.assertIn("signal_expired", r.reasons)


class TestP2DecisionAudit(unittest.TestCase):
    def test_summarize_not_from_sparse_samples(self):
        from review.decision_audit import DecisionAuditLog, DecisionAuditRecord, new_decision_id
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audit.jsonl"
            log = DecisionAuditLog(path=p)
            for i, (cs, dec, st) in enumerate([
                (-11, "WATCH_SHORT", "no_signal"),
                (21, "STANDARD_LONG", "data_unfit"),
                (36, "STANDARD_LONG", "risk_blocked"),
            ]):
                log.append(DecisionAuditRecord(
                    decision_id=new_decision_id(),
                    horizon="short_term",
                    config_version="v_test",
                    content_hash="x",
                    started_at_ms=i,
                    finished_at_ms=i,
                    mark_price=1.0,
                    atr=1.0,
                    cs_final=cs,
                    decision_final=dec,
                    signal_state=st,
                    primary_block="stale" if st == "data_unfit" else ("risk_cap" if st == "risk_blocked" else ""),
                ))
            s = log.summarize()
            self.assertEqual(s["records"], 3)
            self.assertFalse(s["sampling_limited"])
            self.assertEqual(s["signal_ok_count"], 0)
            self.assertIn("stale", s["block_counts"])


class TestP5GuardianIndependent(unittest.TestCase):
    def test_hard_sl_without_score(self):
        from trading.models import HorizonPosition
        from trading.risk_guardian import RiskGuardian
        from unittest.mock import MagicMock

        g = RiskGuardian(MagicMock(), MagicMock())
        pos = HorizonPosition(
            horizon="short_term", side="LONG", entry_price=100.0,
            quantity=0.01, leverage=2, opened_at_ms=1, mark_price=90.0,
            sl_price=95.0,
        )
        act = g._check_hard_sl(pos, 94.0)
        self.assertIsNotNone(act)
        self.assertEqual(act.reason, "hard_sl_guardian")


if __name__ == "__main__":
    unittest.main()
