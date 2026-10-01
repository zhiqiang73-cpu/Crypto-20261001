"""护栏与版本留档单测 — 这是模型输出进入系统前唯一的闸门, 必须钉死."""

import json, sys, os, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.review import PROPOSAL_GUARD, TUNABLE_PARAMS, WEIGHT_GROUPS
from models.review import ParamChange
from review import overrides
from review.review_loop import validate_changes


CUR = {k: (v["min"] + v["max"]) / 2 for k, v in TUNABLE_PARAMS.items()}
# 权重组给一套真实的和为 1 的当前值, 否则归一测试没意义
CUR.update({
    "DIMENSION_WEIGHTS.short_term.news": 0.20,
    "DIMENSION_WEIGHTS.short_term.data": 0.35,
    "DIMENSION_WEIGHTS.short_term.tech": 0.25,
    "DIMENSION_WEIGHTS.short_term.prediction": 0.20,
    "DIMENSION_WEIGHTS.long_term.news": 0.35,
    "DIMENSION_WEIGHTS.long_term.data": 0.25,
    "DIMENSION_WEIGHTS.long_term.tech": 0.10,
    "DIMENSION_WEIGHTS.long_term.prediction": 0.30,
})


class TestGuardWhitelist(unittest.TestCase):
    def test_unknown_param_dropped(self):
        changes, notes = validate_changes(
            [{"param": "DROP TABLE weights", "proposed": 1}], CUR)
        self.assertEqual(changes, [])
        self.assertTrue(any("白名单" in n for n in notes))

    def test_non_numeric_dropped(self):
        changes, _ = validate_changes(
            [{"param": "ADX_BOOST", "proposed": "很大"}], CUR)
        self.assertEqual(changes, [])

    def test_nan_dropped(self):
        changes, _ = validate_changes(
            [{"param": "ADX_BOOST", "proposed": float("nan")}], CUR)
        self.assertEqual(changes, [])

    def test_duplicate_params_collapsed(self):
        changes, notes = validate_changes([
            {"param": "ADX_BOOST", "proposed": 1.3},
            {"param": "ADX_BOOST", "proposed": 1.4},
        ], CUR)
        self.assertEqual(len(changes), 1)
        self.assertTrue(any("重复" in n for n in notes))


class TestGuardClamping(unittest.TestCase):
    def test_absolute_bounds_clamped(self):
        spec = TUNABLE_PARAMS["SAFETY_VALVE_THRESHOLD"]
        cur = CUR["SAFETY_VALVE_THRESHOLD"]
        changes, _ = validate_changes(
            [{"param": "SAFETY_VALVE_THRESHOLD", "proposed": 9999}], CUR)
        # 绝对上限与 ±25% 幅度限同时生效, 取更紧的那个
        expected = min(spec["max"], cur * (1 + PROPOSAL_GUARD["max_relative_change"]))
        self.assertAlmostEqual(changes[0].proposed, expected, places=4)
        self.assertTrue(changes[0].clamped)
        self.assertIn("上限", changes[0].clamp_note)

    def test_relative_change_clamped(self):
        cur = CUR["ADX_BOOST"]
        changes, _ = validate_changes(
            [{"param": "ADX_BOOST", "proposed": cur * 10}], CUR)
        ceiling = cur * (1 + PROPOSAL_GUARD["max_relative_change"])
        # 同时受绝对上限约束, 取更紧的那个
        expected = min(ceiling, TUNABLE_PARAMS["ADX_BOOST"]["max"])
        self.assertAlmostEqual(changes[0].proposed, expected, places=4)
        self.assertTrue(changes[0].clamped)

    def test_small_change_not_clamped(self):
        cur = CUR["ADX_BOOST"]
        changes, _ = validate_changes(
            [{"param": "ADX_BOOST", "proposed": cur * 1.05}], CUR)
        self.assertFalse(changes[0].clamped)
        self.assertAlmostEqual(changes[0].proposed, cur * 1.05, places=4)

    def test_confidence_clamped_to_unit(self):
        changes, _ = validate_changes([
            {"param": "ADX_BOOST", "proposed": CUR["ADX_BOOST"] * 1.02, "confidence": 7},
            {"param": "ADX_DAMPEN", "proposed": CUR["ADX_DAMPEN"] * 0.98, "confidence": -3},
        ], CUR)
        self.assertEqual(changes[0].confidence, 1.0)
        self.assertEqual(changes[1].confidence, 0.0)


class TestGuardConsistency(unittest.TestCase):
    def test_weight_group_renormalized_to_one(self):
        changes, notes = validate_changes([
            {"param": "DIMENSION_WEIGHTS.short_term.data", "proposed": 0.58},
            {"param": "DIMENSION_WEIGHTS.short_term.news", "proposed": 0.05},
        ], CUR)
        by_param = {c.param: c.proposed for c in changes}
        for group, paths in WEIGHT_GROUPS.items():
            total = sum(by_param.get(p, CUR[p]) for p in paths)
            self.assertAlmostEqual(total, 1.0, places=6, msg=f"{group} 之和应为 1.0")
        # 整组四项都要落到输出里, 否则写版本时另外两项会保持旧值, 和又不对了
        for p in WEIGHT_GROUPS["short_term"]:
            self.assertIn(p, by_param)
        self.assertTrue(any("归一" in n for n in notes))

    def test_untouched_group_member_gets_linkage_change(self):
        changes, _ = validate_changes([
            {"param": "DIMENSION_WEIGHTS.long_term.news", "proposed": 0.42},
        ], CUR)
        by_param = {c.param: c for c in changes}
        self.assertIn("DIMENSION_WEIGHTS.long_term.tech", by_param)
        self.assertIn("归一联动", by_param["DIMENSION_WEIGHTS.long_term.tech"].rationale)
        total = sum(by_param[p].proposed for p in WEIGHT_GROUPS["long_term"])
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_renormalized_values_stay_inside_bounds(self):
        changes, _ = validate_changes([
            {"param": "DIMENSION_WEIGHTS.short_term.data", "proposed": 0.60},
            {"param": "DIMENSION_WEIGHTS.short_term.news", "proposed": 0.40},
        ], CUR)
        for c in changes:
            spec = TUNABLE_PARAMS[c.param]
            self.assertGreaterEqual(c.proposed, spec["min"] - 1e-9)
            self.assertLessEqual(c.proposed, spec["max"] + 1e-9)

    def test_threshold_crossing_voids_all_threshold_changes(self):
        # standard_long 与 strong_long 贴得很近时, 一次合法幅度内的上抬就会越界
        near = dict(CUR)
        near["DECISION_THRESHOLDS.standard_long"] = 48.0
        near["DECISION_THRESHOLDS.strong_long"] = 50.0
        changes, notes = validate_changes([
            {"param": "DECISION_THRESHOLDS.standard_long", "proposed": 60.0},
        ], near)
        self.assertEqual([c for c in changes
                          if c.param.startswith("DECISION_THRESHOLDS")], [])
        self.assertTrue(any("交叉" in n for n in notes))

    def test_threshold_crossing_does_not_void_weight_changes(self):
        near = dict(CUR)
        near["DECISION_THRESHOLDS.standard_long"] = 48.0
        near["DECISION_THRESHOLDS.strong_long"] = 50.0
        changes, _ = validate_changes([
            {"param": "DECISION_THRESHOLDS.standard_long", "proposed": 60.0},
            {"param": "ADX_BOOST", "proposed": 1.3},
        ], near)
        self.assertEqual([c.param for c in changes], ["ADX_BOOST"])

    def test_legal_threshold_move_survives(self):
        changes, _ = validate_changes([
            {"param": "DECISION_THRESHOLDS.standard_long", "proposed": 38.0},
        ], CUR)
        self.assertEqual(len(changes), 1)
        self.assertAlmostEqual(changes[0].proposed, 38.0, places=2)

    def test_count_limit_enforced(self):
        raw = [{"param": "ADX_BOOST", "proposed": 1.3},
               {"param": "ADX_DAMPEN", "proposed": 0.5},
               {"param": "BOLL_SQUEEZE_BOOST", "proposed": 1.2},
               {"param": "SAFETY_VALVE_THRESHOLD", "proposed": 55},
               {"param": "DECISION_THRESHOLDS.watch_long", "proposed": 12},
               {"param": "DECISION_THRESHOLDS.strong_long", "proposed": 70}]
        changes, notes = validate_changes(raw, CUR)
        self.assertLessEqual(len(changes), PROPOSAL_GUARD["max_params_per_set"])
        self.assertTrue(any("上限" in n for n in notes))


class TestVersionArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.p_versions = mock.patch.object(overrides, "VERSIONS_DIR", root / "versions")
        self.p_active = mock.patch.object(overrides, "ACTIVE_VERSION_FILE", root / "ACTIVE.json")
        self.p_versions.start(); self.p_active.start()

    def tearDown(self):
        self.p_versions.stop(); self.p_active.stop()
        self.tmp.cleanup()

    def _change(self, param, proposed, current):
        return ParamChange(param=param, current=current, proposed=proposed)

    def test_baseline_created_on_first_commit(self):
        overrides.ensure_baseline()
        self.assertEqual(overrides.active_version_name(), "v1")
        versions = overrides.list_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["kind"], "baseline")

    def test_commit_creates_new_version_and_keeps_old(self):
        base = overrides.flatten_defaults()
        overrides.commit_version([
            self._change("ADX_BOOST", 1.35, base["ADX_BOOST"]),
        ], meta={"proposal_id": "P1", "model": "deepseek-chat", "valid_sample_count": 104})

        self.assertEqual(overrides.active_version_name(), "v2")
        self.assertEqual(len(overrides.list_versions()), 2)
        self.assertAlmostEqual(overrides.effective_params()["ADX_BOOST"], 1.35)
        self.assertAlmostEqual(overrides.flatten_defaults()["ADX_BOOST"],
                               base["ADX_BOOST"])

    def test_rollback_restores_previous_params(self):
        base = overrides.flatten_defaults()
        overrides.commit_version([self._change("ADX_BOOST", 1.4, base["ADX_BOOST"])])
        overrides.rollback("v1")
        self.assertEqual(overrides.active_version_name(), "v1")
        self.assertAlmostEqual(overrides.effective_params()["ADX_BOOST"],
                               base["ADX_BOOST"])

    def test_rollback_missing_version_raises(self):
        with self.assertRaises(FileNotFoundError):
            overrides.rollback("v99")

    def test_advisory_params_not_applied_to_engine(self):
        base = overrides.flatten_defaults()
        overrides.commit_version([self._change("ADX_BOOST", 1.45, base["ADX_BOOST"])])
        self.assertIn("ADX_BOOST", overrides.advisory_params())


class TestEngineOverrideIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.p1 = mock.patch.object(overrides, "VERSIONS_DIR", root / "versions")
        self.p2 = mock.patch.object(overrides, "ACTIVE_VERSION_FILE", root / "ACTIVE.json")
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        self.tmp.cleanup()

    def test_engine_defaults_unchanged_without_overrides(self):
        from engine.scorer import FactorScoringEngine
        from models.signals import StrategyHorizon, DimensionScores
        from utils.scoring import compute_cs_with_boost
        from config.weights import DIMENSION_WEIGHTS
        e = FactorScoringEngine()
        scores = {"news": 8, "data": 54, "tech": 62, "prediction": 20}
        r = e.evaluate(StrategyHorizon.SHORT_TERM,
                       DimensionScores(**scores))
        expected, _ = compute_cs_with_boost(scores, DIMENSION_WEIGHTS["short_term"])
        self.assertAlmostEqual(r.composite_score, round(expected, 2), places=2)

    def test_engine_picks_up_accepted_weights(self):
        from engine.scorer import FactorScoringEngine
        from models.signals import StrategyHorizon, DimensionScores
        from utils.scoring import compute_cs_with_boost
        base = overrides.flatten_defaults()
        overrides.commit_version([
            self._change("DIMENSION_WEIGHTS.short_term.news", 0.05, base["DIMENSION_WEIGHTS.short_term.news"]),
            self._change("DIMENSION_WEIGHTS.short_term.data", 0.50, base["DIMENSION_WEIGHTS.short_term.data"]),
            self._change("DIMENSION_WEIGHTS.short_term.tech", 0.30, base["DIMENSION_WEIGHTS.short_term.tech"]),
            self._change("DIMENSION_WEIGHTS.short_term.prediction", 0.15, base["DIMENSION_WEIGHTS.short_term.prediction"]),
        ])
        e = FactorScoringEngine(use_active_overrides=True)
        self.assertAlmostEqual(e.weights["short_term"]["data"], 0.50)
        self.assertAlmostEqual(e.weights["short_term"]["news"], 0.05)
        scores = {"news": 8, "data": 54, "tech": 62, "prediction": 20}
        r = e.evaluate(StrategyHorizon.SHORT_TERM, DimensionScores(**scores))
        expected, _ = compute_cs_with_boost(scores, e.weights["short_term"])
        self.assertAlmostEqual(r.composite_score, round(expected, 2), places=2)

    def _change(self, param, proposed, current):
        return ParamChange(param=param, current=current, proposed=proposed)


if __name__ == "__main__":
    unittest.main()
