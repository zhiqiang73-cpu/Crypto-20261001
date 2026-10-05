"""配置版本治理验收：完整策略包哈希、不可变、回滚、默认值隔离、生产入口。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.strategy_bundle import (
    bundle_from_document,
    collect_factory_parameters,
    compose_legacy_active_bundle,
    deep_freeze,
    make_bundle,
    parameters_hash,
    validate_parameters,
)
from config.strategy_store import (
    activate_strategy,
    load_active_bundle,
    migrate_weights_active_to_bundle,
    rollback_strategy,
    write_strategy_version,
)
from engine.scorer import FactorScoringEngine
from models.signals import DimensionScores, StrategyHorizon


class TestStrategyBundleCore(unittest.TestCase):
    def test_factory_valid(self):
        p = collect_factory_parameters()
        self.assertIsNone(validate_parameters(p))
        b = make_bundle(strategy_version="t0", parameters=p)
        self.assertTrue(b.load_ok)

    def test_subweight_changes_hash(self):
        p = collect_factory_parameters()
        h0 = parameters_hash(p)
        p2 = copy.deepcopy(p)
        tw = p2["TECH_INDICATOR_WEIGHTS"]["short_term"]
        tw["volume"] = float(tw["volume"]) - 0.01
        tw["macd_hist"] = float(tw["macd_hist"]) + 0.01
        self.assertNotEqual(h0, parameters_hash(p2))

    def test_deep_immutable(self):
        b = make_bundle(strategy_version="imm", parameters=collect_factory_parameters())
        with self.assertRaises(TypeError):
            b.parameters["DIMENSION_WEIGHTS"]["short_term"]["news"] = 0.99  # type: ignore
        pub = b.to_public_dict()
        pub["parameters"]["DIMENSION_WEIGHTS"]["short_term"]["news"] = 0.99
        self.assertNotEqual(
            float(b.parameters["DIMENSION_WEIGHTS"]["short_term"]["news"]),
            0.99,
        )

    def test_same_params_same_hash(self):
        p = collect_factory_parameters()
        self.assertEqual(parameters_hash(p), parameters_hash(copy.deepcopy(p)))

    def test_reject_incomplete_unknown_and_bad_pretrade(self):
        cases = []
        p = collect_factory_parameters(); p["DIMENSION_WEIGHTS"] = {}; cases.append(p)
        p = collect_factory_parameters(); p["MAPPING"] = {}; cases.append(p)
        p = collect_factory_parameters(); p["PRETRADE_LIMITS"]["signal_ttl_sec"] = -1; cases.append(p)
        p = collect_factory_parameters(); p["UNKNOWN_BEHAVIOR"] = 1; cases.append(p)
        for params in cases:
            self.assertIsNotNone(validate_parameters(params))

    def test_document_requires_sealed_identity_fields(self):
        b = make_bundle(strategy_version="sealed", parameters=collect_factory_parameters())
        for field in ("parameters_hash", "implementation_id", "strategy_identity"):
            doc = b.to_public_dict()
            doc.pop(field)
            self.assertFalse(bundle_from_document(doc).load_ok, field)


class TestActivateRollback(unittest.TestCase):
    def test_activate_rollback_full_params(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "ACTIVE.json"
            p1 = collect_factory_parameters()
            b1 = make_bundle(strategy_version="sb_a", parameters=p1, change_reason="a")
            write_strategy_version(b1, dir_path=root)

            p2 = copy.deepcopy(p1)
            p2["DIMENSION_WEIGHTS"]["short_term"]["news"] = 0.18
            p2["DIMENSION_WEIGHTS"]["short_term"]["data"] = 0.37
            p2["TECH_INDICATOR_WEIGHTS"]["short_term"]["volume"] = (
                float(p2["TECH_INDICATOR_WEIGHTS"]["short_term"]["volume"]) - 0.01
            )
            p2["TECH_INDICATOR_WEIGHTS"]["short_term"]["macd_hist"] = (
                float(p2["TECH_INDICATOR_WEIGHTS"]["short_term"]["macd_hist"]) + 0.01
            )
            p2["RISK_PER_TRADE_PCT"]["short_term"] = 0.006
            # tweak mapping scalar
            p2["MAPPING"]["BLACK_SWAN_LIQ_5M_USD"] = float(p2["MAPPING"]["BLACK_SWAN_LIQ_5M_USD"]) * 1.0
            # change an anchor endpoint slightly
            anchors = p2["MAPPING"]["FUNDING_RATE_ANCHORS"]
            anchors[0] = [float(anchors[0][0]), float(anchors[0][1]) + 0.0]
            # real change:
            p2["MAPPING"]["FUNDING_RATE_ANCHORS"] = [
                [float(a[0]), float(a[1]) + (0.1 if i == 0 else 0.0)] for i, a in enumerate(anchors)
            ]
            b2 = make_bundle(strategy_version="sb_b", parameters=p2, parent_version="sb_a")
            self.assertTrue(b2.load_ok, b2.load_error)
            write_strategy_version(b2, dir_path=root)

            activate_strategy("sb_a", dir_path=root, active_file=active, require_impl_match=True)
            loaded = load_active_bundle(dir_path=root, active_file=active, allow_legacy_compose=False)
            self.assertEqual(loaded.parameters_hash, b1.parameters_hash)

            activate_strategy("sb_b", dir_path=root, active_file=active)
            loaded2 = load_active_bundle(dir_path=root, active_file=active, allow_legacy_compose=False)
            self.assertEqual(loaded2.parameters_hash, b2.parameters_hash)
            self.assertEqual(
                float(loaded2.parameters["TECH_INDICATOR_WEIGHTS"]["short_term"]["macd_hist"]),
                float(p2["TECH_INDICATOR_WEIGHTS"]["short_term"]["macd_hist"]),
            )
            self.assertEqual(float(loaded2.parameters["RISK_PER_TRADE_PCT"]["short_term"]), 0.006)

            rollback_strategy("sb_a", dir_path=root, active_file=active)
            loaded3 = load_active_bundle(dir_path=root, active_file=active, allow_legacy_compose=False)
            self.assertEqual(loaded3.parameters_hash, b1.parameters_hash)
            self.assertEqual(
                float(loaded3.parameters["RISK_PER_TRADE_PCT"]["short_term"]),
                float(p1["RISK_PER_TRADE_PCT"]["short_term"]),
            )

    def test_reject_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            b = make_bundle(strategy_version="sb_bad", parameters=collect_factory_parameters())
            doc = b.to_public_dict()
            doc["parameters_hash"] = "0" * 64
            path = root / "sb_bad.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            from config.strategy_bundle import bundle_from_document
            bad = bundle_from_document(doc, verify_hash=True)
            self.assertFalse(bad.load_ok)
            with self.assertRaises(ValueError):
                activate_strategy("sb_bad", dir_path=root, active_file=root / "ACTIVE.json")

    def test_corrupt_or_missing_active_is_invalid_not_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "ACTIVE.json"
            active.write_text("{", encoding="utf-8")
            self.assertFalse(load_active_bundle(dir_path=root, active_file=active).load_ok)
            active.write_text(json.dumps({"strategy_version": "missing"}), encoding="utf-8")
            self.assertFalse(load_active_bundle(dir_path=root, active_file=active).load_ok)


class TestDefaultIsolation(unittest.TestCase):
    def test_activated_bundle_ignores_code_default_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "ACTIVE.json"
            p = collect_factory_parameters()
            b = make_bundle(strategy_version="sb_iso", parameters=p)
            write_strategy_version(b, dir_path=root)
            activate_strategy("sb_iso", dir_path=root, active_file=active)
            loaded = load_active_bundle(dir_path=root, active_file=active, allow_legacy_compose=False)
            tech0 = float(loaded.parameters["TECH_INDICATOR_WEIGHTS"]["short_term"]["volume"])
            # 模拟代码默认被改：不应影响已激活包
            with mock.patch.dict(
                "config.weights.TECH_INDICATOR_WEIGHTS",
                {
                    **__import__("config.weights", fromlist=["TECH_INDICATOR_WEIGHTS"]).TECH_INDICATOR_WEIGHTS,
                },
                clear=False,
            ):
                # 直接改模块对象
                import config.weights as W
                old = W.TECH_INDICATOR_WEIGHTS["short_term"]["volume"]
                W.TECH_INDICATOR_WEIGHTS["short_term"]["volume"] = 0.99
                try:
                    loaded2 = load_active_bundle(
                        dir_path=root, active_file=active, allow_legacy_compose=False
                    )
                    self.assertEqual(
                        float(loaded2.parameters["TECH_INDICATOR_WEIGHTS"]["short_term"]["volume"]),
                        tech0,
                    )
                finally:
                    W.TECH_INDICATOR_WEIGHTS["short_term"]["volume"] = old


class TestProductionPathUsesBundle(unittest.TestCase):
    def test_mapper_and_engine_use_strategy_params(self):
        from mappers.tech_mapper import TechFactorMapper
        from models.snapshots import TechSnapshot

        p = collect_factory_parameters()
        # 改变 tech 权重应改变结果
        p2 = copy.deepcopy(p)
        tw = p2["TECH_INDICATOR_WEIGHTS"]["short_term"]
        # put all weight on rsi_divergence
        keys = list(tw.keys())
        for k in keys:
            tw[k] = 0.0
        tw["rsi_divergence"] = 1.0

        snap = TechSnapshot(
            available=True,
            structure_score=0.0,
            ema_score=0.0,
            vp_score=0.0,
            sr_score=0.0,
            vwap_score=0.0,
            pdh_pdl_score=0.0,
            rsi_divergence_score=80.0,
            macd_score=0.0,
            volume_score=-100.0,
            obv_score=0.0,
            pin_bar_score=0.0,
            engulfing_score=0.0,
            inside_bar_score=0.0,
            adx=30.0,
            boll_squeeze=False,
        )
        m = TechFactorMapper()
        r1 = m.map(snap, StrategyHorizon.SHORT_TERM, strategy_params=p)
        r2 = m.map(snap, StrategyHorizon.SHORT_TERM, strategy_params=p2)
        self.assertNotEqual(r1.s_tech, r2.s_tech)
        self.assertAlmostEqual(r2.s_tech, 80.0, delta=1.0)

        eng = FactorScoringEngine(use_active_overrides=False)
        from config.effective_config import apply_config_to_engine, freeze_effective_config
        from config.strategy_bundle import make_bundle as mb
        b = mb(strategy_version="sb_eng", parameters=p2)
        from config.effective_config import _from_bundle
        cfg = _from_bundle(b, 1)
        apply_config_to_engine(eng, cfg)
        self.assertEqual(eng.th["standard_long"], p2["DECISION_THRESHOLDS"]["standard_long"])

    def test_sensitivity_mapper_without_params_reads_module(self):
        """临时证明：若不传 strategy_params，会退回模块常量（回归哨兵）。"""
        from mappers.tech_mapper import TechFactorMapper
        from models.snapshots import TechSnapshot
        import config.weights as W

        snap = TechSnapshot(
            available=True,
            structure_score=10, ema_score=0, vp_score=0, sr_score=0, vwap_score=0,
            pdh_pdl_score=0, rsi_divergence_score=0, macd_score=0, volume_score=0,
            obv_score=0, pin_bar_score=0, engulfing_score=0, inside_bar_score=0,
            adx=25.0, boll_squeeze=False,
        )
        m = TechFactorMapper()
        r_mod = m.map(snap, StrategyHorizon.SHORT_TERM)  # 无 params
        p = collect_factory_parameters()
        r_bundle = m.map(snap, StrategyHorizon.SHORT_TERM, strategy_params=p)
        self.assertEqual(r_mod.s_tech, r_bundle.s_tech)

    def test_mapping_anchor_is_consumed_by_mapper(self):
        from mappers.data_mapper import DataFactorMapper
        from models.snapshots import DataSnapshot, BinanceMicroSnapshot
        p = collect_factory_parameters()
        p["MAPPING"]["FUNDING_RATE_ANCHORS"] = [[-1.0, 99.0], [1.0, 99.0]]
        snap = DataSnapshot(binance=BinanceMicroSnapshot(funding_rate_annualized=0.1))
        out = DataFactorMapper().map(
            snap, StrategyHorizon.SHORT_TERM, strategy_params=p
        )
        self.assertEqual(out.indicator_scores["funding_rate"], 99.0)

    def test_position_exit_uses_entry_snapshot(self):
        import time
        from trading.exit_checker import ExitChecker
        from trading.models import HorizonPosition
        cfg = {
            "hard_sl_atr": 2.0, "hard_sl_pct": 0.20,
            "spread_force_mult": 3.0, "liq_force_usd": 100_000_000,
            "tp1_atr": 4.0, "tp1_close_pct": 0.5,
            "tp2_atr": 5.0, "tp2_close_pct": 0.3,
            "trailing_atr": 2.0, "cs_decay_threshold": 99,
            "time_stop_min": None,
        }
        pos = HorizonPosition(
            horizon="short_term", side="LONG", entry_price=100.0,
            quantity=1.0, entry_atr=10.0, opened_at_ms=int(time.time() * 1000),
            config_snapshot={"exit_strategy": {"short_term": cfg}},
        )
        self.assertEqual(ExitChecker().check_exits(pos, 85.0, 50.0), [])
        out = ExitChecker().check_exits(pos, 79.0, 50.0)
        self.assertEqual(out[0].reason, "hard_sl")


class TestRiskUsesSealedConfig(unittest.IsolatedAsyncioTestCase):
    async def test_calc_qty_uses_sealed_risk_and_stop(self):
        from trading.fake_exchange import FakeBinanceClient
        from trading.position_manager import PositionManager
        with tempfile.TemporaryDirectory() as td:
            mgr = PositionManager(
                FakeBinanceClient(equity=10_000, mark=100),
                persist_path=Path(td) / "positions.json",
            )
            params = collect_factory_parameters()
            params["RISK_PER_TRADE_PCT"]["short_term"] = 0.01
            params["EXIT_STRATEGY"]["short_term"]["hard_sl_atr"] = 2.0
            qty, risk, distance = await mgr._calc_qty(
                "short_term", 100.0, atr=10.0, strategy_params=params
            )
            self.assertAlmostEqual(distance, 20.0)
            self.assertLessEqual(risk, 100.0)
            self.assertGreater(qty, 0)


class TestMigration(unittest.TestCase):
    def test_migrate_does_not_overwrite_v1(self):
        v1 = Path("config/weights_versions/v1.json")
        before = v1.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            b = migrate_weights_active_to_bundle(
                new_version="sb_m_test", dir_path=Path(td), write=True
            )
            self.assertTrue(b.load_ok, b.load_error)
            self.assertTrue((Path(td) / "sb_m_test.json").exists())
            self.assertIn("不能冒充", b.migration_note)
            self.assertTrue(b.unknown_historical_fields)
        after = v1.read_text(encoding="utf-8")
        self.assertEqual(before, after)


class TestMidDecisionIsolation(unittest.TestCase):
    def test_frozen_cfg_not_affected_by_later_activate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "ACTIVE.json"
            p1 = collect_factory_parameters()
            p2 = copy.deepcopy(p1)
            p2["DECISION_THRESHOLDS"]["standard_long"] = 40.0
            b1 = make_bundle(strategy_version="sb_d1", parameters=p1)
            b2 = make_bundle(strategy_version="sb_d2", parameters=p2)
            write_strategy_version(b1, dir_path=root)
            write_strategy_version(b2, dir_path=root)
            activate_strategy("sb_d1", dir_path=root, active_file=active)
            from config.effective_config import freeze_effective_config
            cfg = freeze_effective_config(
                allow_factory_fallback=False,
                bundle=load_active_bundle(dir_path=root, active_file=active, allow_legacy_compose=False),
            )
            th1 = float(cfg.decision_thresholds["standard_long"])
            activate_strategy("sb_d2", dir_path=root, active_file=active)
            # 旧快照不变
            self.assertEqual(float(cfg.decision_thresholds["standard_long"]), th1)
            self.assertEqual(th1, float(p1["DECISION_THRESHOLDS"]["standard_long"]))


if __name__ == "__main__":
    unittest.main()
