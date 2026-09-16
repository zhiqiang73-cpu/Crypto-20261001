#!/usr/bin/env python3
"""生成完整策略包迁移文件（不切换 ACTIVE，不改数值）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.strategy_store import migrate_weights_active_to_bundle, STRATEGY_VERSIONS_DIR


def main() -> int:
    STRATEGY_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # 版本不可覆盖；自动选择下一个迁移序号。
    n = 1
    while (STRATEGY_VERSIONS_DIR / f"sb_m{n}_current_baseline.json").exists():
        n += 1
    ver = f"sb_m{n}_current_baseline"
    target = STRATEGY_VERSIONS_DIR / f"{ver}.json"
    b = migrate_weights_active_to_bundle(new_version=ver, write=True)
    print("wrote", target)
    print("load_ok", b.load_ok, b.load_error)
    print("parameters_hash", b.parameters_hash)
    print("implementation_id", b.implementation_id[:24])
    print("strategy_identity", b.strategy_identity[:24])
    print("unknown", b.unknown_historical_fields)
    print("NOTE: ACTIVE not changed. Review then activate manually if desired.")
    return 0 if b.load_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
