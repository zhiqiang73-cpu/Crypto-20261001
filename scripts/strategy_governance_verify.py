#!/usr/bin/env python3
"""策略配置治理离线验收入口（不下单、不改 ACTIVE）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from config.strategy_bundle import compose_legacy_active_bundle, collect_factory_parameters, parameters_hash
    from config.strategy_store import STRATEGY_ACTIVE_FILE, active_strategy_name
    from config.effective_config import freeze_effective_config

    legacy = compose_legacy_active_bundle()
    cfg = freeze_effective_config(allow_factory_fallback=False)
    print("strategy_ACTIVE_file", STRATEGY_ACTIVE_FILE.exists(), active_strategy_name())
    print("legacy_compose version", legacy.strategy_version)
    print("freeze version", cfg.version, "load_ok", cfg.load_ok)
    print("parameters_hash", (cfg.parameters_hash or cfg.content_hash)[:24])
    print("implementation_id", (cfg.implementation_id or "")[:24])
    print("strategy_identity", (cfg.strategy_identity or "")[:24])
    print("migration_note", (cfg.migration_note or "")[:80])

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_strategy_governance")
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    report = {
        "config_governance_offline": "PASS" if result.wasSuccessful() else "FAIL",
        "strategy_active_full_bundle": bool(active_strategy_name()),
        "current_compose": cfg.version,
        "parameters_hash": cfg.parameters_hash or cfg.content_hash,
        "implementation_id": cfg.implementation_id,
        "strategy_identity": cfg.strategy_identity,
        "tests_run": result.testsRun,
        "failures": len(result.failures) + len(result.errors),
        "note": (
            "本任务只验收配置治理。不代表门槛合理、策略盈利或适合实盘。"
            "完整包和 ACTIVE 状态以 strategy_active_full_bundle/current_compose 为准。"
        ),
    }
    out = ROOT / "docs" / "strategy_governance_verify_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
