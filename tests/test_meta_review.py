"""元评审收敛控制单元测试."""

from __future__ import annotations

import unittest

from models.review import ParamChange, ProposalSet
from review import meta_review


class TestMetaReview(unittest.TestCase):
    def test_learning_rate_decay(self):
        r0 = meta_review.current_max_relative_change(0)
        r1 = meta_review.current_max_relative_change(1)
        r20 = meta_review.current_max_relative_change(20)
        self.assertAlmostEqual(r0, 0.25)
        self.assertLess(r1, r0)
        self.assertGreaterEqual(r20, 0.05)
        self.assertAlmostEqual(r20, 0.05)

    def test_oscillation_detect(self):
        versions = [
            {"kind": "tuned", "changes": [{"param": "SAFETY_VALVE_THRESHOLD", "delta": 5}]},
            {"kind": "tuned", "changes": [{"param": "SAFETY_VALVE_THRESHOLD", "delta": -4}]},
            {"kind": "tuned", "changes": [{"param": "SAFETY_VALVE_THRESHOLD", "delta": 3}]},
            {"kind": "tuned", "changes": [{"param": "SAFETY_VALVE_THRESHOLD", "delta": -2}]},
        ]
        locked = meta_review.detect_oscillation(versions)
        self.assertIn("SAFETY_VALVE_THRESHOLD", locked)

    def test_gate_observation_mode(self):
        state = meta_review.MetaState(observation_mode=True)
        prop = ProposalSet(
            proposal_id="P1",
            created_at_ms=1,
            model="x",
            valid_sample_count=100,
            changes=[ParamChange(param="SAFETY_VALVE_THRESHOLD", current=50, proposed=55)],
            status="pending",
        )
        gated, st, note = meta_review.gate_proposal(prop, {"SAFETY_VALVE_THRESHOLD": 50}, state)
        self.assertEqual(gated.status, "blocked")
        self.assertIn("观察", note)

    def test_filter_locked(self):
        state = meta_review.MetaState(locked_params={"SAFETY_VALVE_THRESHOLD": 2})
        prop = ProposalSet(
            proposal_id="P2",
            created_at_ms=1,
            model="x",
            valid_sample_count=100,
            changes=[
                ParamChange(param="SAFETY_VALVE_THRESHOLD", current=50, proposed=55, confidence=0.9),
                ParamChange(param="ADX_BOOST", current=1.2, proposed=1.25, confidence=0.8),
            ],
            status="pending",
        )
        out = meta_review.filter_locked_changes(prop, state)
        self.assertEqual(len(out.changes), 1)
        self.assertEqual(out.changes[0].param, "ADX_BOOST")


if __name__ == "__main__":
    unittest.main()
