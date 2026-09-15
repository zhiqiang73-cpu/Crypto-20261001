#!/usr/bin/env python3
"""Round-4 离线验收入口（不连交易所、不改 ACTIVE、不下单）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    print("=== Round-4 offline verify ===")
    from config.effective_config import freeze_effective_config
    from config.weights import DECISION_THRESHOLDS, DIMENSION_WEIGHTS, DERIVATIVES_INDICATOR_WEIGHTS
    from review.overrides import active_version_name, effective_params

    active = active_version_name()
    flat = effective_params()
    cfg = freeze_effective_config(allow_factory_fallback=False)
    print(f"ACTIVE={active} load_ok={cfg.load_ok} version={cfg.version}")
    print(f"engine_th.standard_long={cfg.decision_thresholds.get('standard_long')}")
    print(f"factory_th.standard_long={DECISION_THRESHOLDS.get('standard_long')}")
    print(f"engine_w.short={cfg.dimension_weights.get('short_term')}")
    print(f"factory_w.short={DIMENSION_WEIGHTS.get('short_term')}")
    print(f"heatmap_weight_short={DERIVATIVES_INDICATOR_WEIGHTS['short_term']['liquidation_heatmap']}")

    mismatch = (
        cfg.decision_thresholds.get("standard_long") != DECISION_THRESHOLDS.get("standard_long")
        or cfg.dimension_weights["short_term"] != DIMENSION_WEIGHTS["short_term"]
    )
    if mismatch:
        print("NOTE: ACTIVE 与 weights.py 出厂仍漂移 — 属预期直至部署切 v2；")
        print("      生产路径必须以 freeze_effective_config/快照为准，面板不得再读模块常量。")

    # 跑 round4 + 关键回归子集
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromName("tests.test_round4_acceptance"))
    suite.addTests(loader.loadTestsFromName("tests.test_round3_acceptance"))
    suite.addTests(loader.loadTestsFromName("tests.test_data_mapper"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    report = {
        "engineering_offline": "PASS" if result.wasSuccessful() else "FAIL",
        "testnet_execution": "UNVERIFIED",
        "strategy_oos_edge": "INSUFFICIENT_EVIDENCE",
        "live_ready": "NOT_SATISFIED",
        "active_version": active,
        "effective_standard_long": cfg.decision_thresholds.get("standard_long"),
        "factory_standard_long": DECISION_THRESHOLDS.get("standard_long"),
        "failures": len(result.failures) + len(result.errors),
        "tests_run": result.testsRun,
    }
    out = ROOT / "docs" / "round4_verify_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Wrote", out)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
