#!/usr/bin/env python3
"""按当前实现指纹重新封存 ACTIVE 策略，修复 implementation_id 漂移。

用法：
    python3 -m scripts.reseal_strategy --reason "round5 code changes" --dry-run
    python3 -m scripts.reseal_strategy --reason "round5 code changes"

这是治理动作：会生成新的策略版本文件并切换 ACTIVE，历史版本保持不变。
先看 --dry-run 输出，确认要封存的内容再执行正式封存。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.strategy_bundle import compute_implementation_id
from config.strategy_store import (
    active_strategy_name,
    load_active_bundle,
    reseal_active_strategy,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reason", default="implementation_id_reseal")
    parser.add_argument("--note", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    current = compute_implementation_id()
    active_name = active_strategy_name()
    active = load_active_bundle()

    print(f"ACTIVE strategy : {active_name}")
    print(f"current impl id : {current}")
    print(f"active impl id  : {active.implementation_id or '(none)'}")
    print(f"load_ok         : {active.load_ok}")
    if not active.load_ok:
        print(f"load_error      : {active.load_error}")

    drift = bool(active.implementation_id) and active.implementation_id != current
    print(f"drift detected  : {drift}")

    if not drift:
        print("无需重新封存：实现指纹已一致。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 将执行：")
        print(f"  parent_version = {active_name}")
        print("  参数继承当前 ACTIVE，implementation_id 更新为当前指纹")
        print("  生成新的 sb_reseal_<ts> 版本并切换 ACTIVE（历史版本不变）")
        return 0

    bundle = reseal_active_strategy(reason=args.reason, note=args.note)
    print(f"\n已重新封存：{bundle.strategy_version}")
    print(f"新 implementation_id：{bundle.implementation_id}")
    print(f"parameters_hash     ：{bundle.parameters_hash}")
    print(f"strategy_identity   ：{bundle.strategy_identity}")

    reloaded = load_active_bundle()
    print(f"\n复核 load_ok：{reloaded.load_ok}")
    if not reloaded.load_ok:
        print(f"复核 load_error：{reloaded.load_error}")
        return 1
    print("对账通过：ACTIVE 策略已与当前实现指纹一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
