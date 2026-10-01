"""统计复盘引擎测试 — 验证「无 LLM 也能跑通自我递归改进闭环」.

覆盖:
  * 四条统计规则各自的触发与不触发条件
  * 与 validate_changes 护栏的集成 (权重和仍恒为 1.0)
  * 确定性 (同输入必同输出)
  * 端到端: 不配置任何 API Key、不发任何网络请求也能产出建议
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from config.review import PROPOSAL_GUARD, TUNABLE_PARAMS, WEIGHT_GROUPS
from models.review import SettleStatus, TradeRecord, now_ms
from review import overrides, review_loop, statistical_review as sr
from review.stats import compute_stats


def make_record(
    *,
    horizon: str = "short_term",
    decision: str = "standard_long",
    direction: str = "LONG",
    correct: bool = True,
    scores: dict | None = None,
    mfe: float = 1.5,
    mae: float = 0.5,
    trade_id: str | None = None,
) -> TradeRecord:
    rec = TradeRecord(
        trade_id=trade_id or f"T{now_ms()}-{id(object()) % 100000}",
        opened_at_ms=now_ms(),
        horizon=horizon,
        entry_price=60000.0,
        scores=scores or {"news": 10.0, "data": 20.0, "tech": 30.0, "prediction": 5.0},
        # direction 是只读属性, 由 composite_score 的符号推导
        composite_score=55.0 if direction == "LONG" else -55.0,
        decision=decision,
        atr=400.0,
    )
    rec.status = (SettleStatus.CORRECT if correct else SettleStatus.WRONG).value
    rec.settled_at_ms = rec.opened_at_ms + 3_600_000
    rec.max_favorable_atr = mfe
    rec.max_adverse_atr = mae
    return rec


def build_biased_records(n: int = 60) -> list:
    """构造一批「技术面能区分赢单、消息面反向」的档案.

    赢单: 技术面读数高、消息面低;  错单: 反过来。
    这样 delta(tech) > 0、delta(news) < 0, 两条方向都可验证。
    """
    records = []
    for i in range(n):
        correct = i % 2 == 0
        if correct:
            scores = {"news": 5.0, "data": 20.0, "tech": 45.0, "prediction": 10.0}
        else:
            scores = {"news": 40.0, "data": 20.0, "tech": 8.0, "prediction": 10.0}
        records.append(make_record(correct=correct, scores=scores, trade_id=f"B{i}"))
    return records


class StatisticalEngineBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        overrides.ensure_baseline()

    def setUp(self) -> None:
        self.current = overrides.effective_params()


class TestWeightRule(StatisticalEngineBase):
    def test_insufficient_sample_produces_no_weight_change(self) -> None:
        records = build_biased_records(10)          # < MIN_SAMPLE_FOR_WEIGHT_RULE
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        weight_changes = [
            c for c in produced.changes
            if any(c["param"] in paths for paths in WEIGHT_GROUPS.values())
        ]
        self.assertEqual(weight_changes, [])
        self.assertIn("未达门槛", produced.diagnosis)

    def test_informative_face_gains_and_anti_face_loses(self) -> None:
        records = build_biased_records(60)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        by_param = {c["param"]: c for c in produced.changes}

        tech = by_param.get("DIMENSION_WEIGHTS.short_term.tech")
        news = by_param.get("DIMENSION_WEIGHTS.short_term.news")
        self.assertIsNotNone(tech, "技术面应被调整")
        self.assertIsNotNone(news, "消息面应被调整")
        self.assertGreater(tech["proposed"], tech["current"])
        self.assertLess(news["proposed"], news["current"])

    def test_weight_changes_survive_guard_and_sum_to_one(self) -> None:
        records = build_biased_records(60)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        changes, _notes = review_loop.validate_changes(produced.changes, self.current)
        merged = dict(self.current)
        for c in changes:
            merged[c.param] = c.proposed
        for _group, paths in WEIGHT_GROUPS.items():
            if any(c.param in paths for c in changes):
                total = sum(merged[p] for p in paths)
                self.assertAlmostEqual(total, 1.0, places=6)

    def test_relative_tilt_not_absolute_level(self) -> None:
        """所有面读数整体抬高不应产生任何权重改动 (只反映市场状态)."""
        records = []
        for i in range(60):
            correct = i % 2 == 0
            # 赢单与错单在「相对高低」上完全一致, 只是整体水平不同
            scores = {"news": 30.0, "data": 40.0, "tech": 50.0, "prediction": 35.0}
            records.append(make_record(correct=correct, scores=scores, trade_id=f"U{i}"))
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        weight_changes = [
            c for c in produced.changes
            if any(c["param"] in paths for paths in WEIGHT_GROUPS.values())
        ]
        self.assertEqual(weight_changes, [])

    def test_extreme_magnitude_penalty_is_recorded(self) -> None:
        """错单里读数幅度明显更大的面, 调整幅度应被打折并写进理由."""
        # 需要至少两个面有判别力, 权重规则才会启动 (单面无法判断相对高低)
        records = []
        for i in range(60):
            correct = i % 2 == 0
            if correct:
                scores = {"news": 5.0, "data": 20.0, "tech": 44.0, "prediction": 10.0}
            else:
                scores = {"news": 45.0, "data": 20.0, "tech": -80.0, "prediction": 10.0}
            records.append(make_record(correct=correct, scores=scores, trade_id=f"E{i}"))
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        tech = next(
            (c for c in produced.changes
             if c["param"] == "DIMENSION_WEIGHTS.short_term.tech"), None)
        self.assertIsNotNone(tech)
        self.assertIn("读得越极端越容易错", tech["rationale"])


class TestThresholdRule(StatisticalEngineBase):
    def _tier_records(self, tier: str, correct_count: int, wrong_count: int) -> list:
        records = []
        for i in range(correct_count):
            records.append(make_record(decision=tier, correct=True, trade_id=f"{tier}C{i}"))
        for i in range(wrong_count):
            records.append(make_record(decision=tier, correct=False, trade_id=f"{tier}W{i}"))
        return records

    def test_losing_long_tier_tightens_threshold_upward(self) -> None:
        records = self._tier_records("standard_long", correct_count=4, wrong_count=20)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        change = next(
            (c for c in produced.changes
             if c["param"] == "DECISION_THRESHOLDS.standard_long"), None)
        self.assertIsNotNone(change)
        self.assertGreater(change["proposed"], change["current"])
        self.assertIn("收紧", change["rationale"])

    def test_losing_short_tier_tightens_threshold_downward(self) -> None:
        records = self._tier_records("standard_short", correct_count=4, wrong_count=20)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        change = next(
            (c for c in produced.changes
             if c["param"] == "DECISION_THRESHOLDS.standard_short"), None)
        self.assertIsNotNone(change)
        self.assertLess(change["proposed"], change["current"])

    def test_winning_tier_loosens_threshold(self) -> None:
        records = self._tier_records("strong_long", correct_count=22, wrong_count=3)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        change = next(
            (c for c in produced.changes
             if c["param"] == "DECISION_THRESHOLDS.strong_long"), None)
        self.assertIsNotNone(change)
        self.assertLess(change["proposed"], change["current"])
        self.assertIn("放宽", change["rationale"])

    def test_neutral_tier_untouched(self) -> None:
        records = self._tier_records("standard_long", correct_count=12, wrong_count=12)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        params = {c["param"] for c in produced.changes}
        self.assertNotIn("DECISION_THRESHOLDS.standard_long", params)


class TestSafetyValveRule(StatisticalEngineBase):
    def test_low_win_rate_raises_valve(self) -> None:
        records = [make_record(correct=(i < 5), trade_id=f"L{i}") for i in range(40)]
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        change = next(
            (c for c in produced.changes if c["param"] == "SAFETY_VALVE_THRESHOLD"), None)
        self.assertIsNotNone(change)
        self.assertGreater(change["proposed"], change["current"])

    def test_high_win_rate_lowers_valve(self) -> None:
        records = [make_record(correct=(i < 34), trade_id=f"H{i}") for i in range(40)]
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        change = next(
            (c for c in produced.changes if c["param"] == "SAFETY_VALVE_THRESHOLD"), None)
        self.assertIsNotNone(change)
        self.assertLess(change["proposed"], change["current"])


class TestGuardIntegration(StatisticalEngineBase):
    def test_candidate_count_respects_guard_limit(self) -> None:
        records = build_biased_records(80)
        # 加入多个档位的强信号, 逼出超过上限的候选
        for tier, cc, wc in (("strong_long", 3, 20), ("standard_short", 3, 20),
                             ("watch_long", 3, 20)):
            records.extend(
                [make_record(decision=tier, correct=True, trade_id=f"{tier}C{i}")
                 for i in range(cc)]
                + [make_record(decision=tier, correct=False, trade_id=f"{tier}W{i}")
                   for i in range(wc)]
            )
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        limit = PROPOSAL_GUARD["max_params_per_set"]
        self.assertLessEqual(len(produced.changes), limit)
        if len(produced.changes) == limit:
            self.assertTrue(any("超过护栏上限" in n for n in produced.notes))

    def test_all_proposed_params_are_in_whitelist(self) -> None:
        records = build_biased_records(80)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        for c in produced.changes:
            self.assertIn(c["param"], TUNABLE_PARAMS)

    def test_guard_clamps_out_of_range_proposals(self) -> None:
        records = build_biased_records(60)
        stats = compute_stats(records)
        produced = sr.propose_from_stats(stats, self.current, records)
        changes, _ = review_loop.validate_changes(produced.changes, self.current)
        for c in changes:
            spec = TUNABLE_PARAMS[c.param]
            self.assertGreaterEqual(c.proposed, spec["min"] - 1e-9)
            self.assertLessEqual(c.proposed, spec["max"] + 1e-9)


class TestDeterminism(StatisticalEngineBase):
    def test_same_input_same_output(self) -> None:
        records = build_biased_records(60)
        stats = compute_stats(records)
        a = sr.propose_from_stats(stats, self.current, records)
        b = sr.propose_from_stats(stats, self.current, records)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_annotate_record_is_deterministic(self) -> None:
        rec = make_record(correct=False)
        first = sr.annotate_record(rec)
        second = sr.annotate_record(rec)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("[统计]"))

    def test_build_daily_summary_shape(self) -> None:
        records = build_biased_records(40)
        stats = compute_stats(records)
        out = sr.build_daily_summary("2026-10-01", records, stats, {"short_term.tech": 0.3})
        self.assertEqual(out["engine"], "statistical")
        self.assertEqual(out["date"], "2026-10-01")
        self.assertEqual(out["sample"]["valid"], stats.valid)
        self.assertIn("note", out)
        # 可 JSON 序列化 (日终要落盘)
        json.dumps(out, ensure_ascii=False)


class TestEndToEndWithoutKey(unittest.TestCase):
    """关键验收: 不配置任何 API Key、不发任何网络请求也能跑完整个复盘."""

    @classmethod
    def setUpClass(cls) -> None:
        overrides.ensure_baseline()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = review_loop.PROPOSALS_DIR
        review_loop.PROPOSALS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        review_loop.PROPOSALS_DIR = self._orig
        self._tmp.cleanup()

    def test_run_review_needs_no_client(self) -> None:
        records = build_biased_records(60)
        proposal = asyncio.run(review_loop.run_review(records, force=True))
        self.assertEqual(proposal.model, review_loop.ENGINE_NAME)
        self.assertEqual(proposal.model, "statistical")
        self.assertEqual(proposal.valid_sample_count, 60)
        self.assertTrue(proposal.diagnosis)

    def test_run_review_blocks_below_sample_target(self) -> None:
        records = build_biased_records(10)
        proposal = asyncio.run(review_loop.run_review(records))
        self.assertEqual(proposal.status, "blocked")
        self.assertIn("有效样本", proposal.blocked_reason)

    def test_run_review_force_bypasses_gate(self) -> None:
        records = build_biased_records(10)
        proposal = asyncio.run(review_loop.run_review(records, force=True))
        self.assertNotEqual(proposal.status, "blocked")

    def test_annotate_error_writes_note_without_client(self) -> None:
        rec = make_record(correct=False)
        out = asyncio.run(review_loop.annotate_error(rec, journal=None))
        self.assertTrue(out.model_note)
        self.assertTrue(out.model_note.startswith("[统计]"))

    def test_no_network_module_is_imported_by_engine(self) -> None:
        """统计引擎不得依赖任何 HTTP 客户端."""
        import review.statistical_review as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("aiohttp", "requests", "urllib", "httpx", "openai"):
            self.assertNotIn(f"import {forbidden}", src)
            self.assertNotIn(f"from {forbidden}", src)


if __name__ == "__main__":
    unittest.main()
