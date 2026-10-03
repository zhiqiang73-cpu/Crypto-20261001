#!/usr/bin/env python3
"""第三轮离线可重复验收入口 — 不加载密钥、不下单、不连测试网账户。

用法:
  cd /Users/zengyun/我的AI/crypto
  python3 scripts/round3_verify.py
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 拒绝意外加载密钥文件
for k in list(os.environ):
    if "SECRET" in k.upper() or "API_KEY" in k.upper() or "BINANCE" in k.upper():
        # 不删除用户环境，但本脚本不读取
        pass


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromName("tests.test_round3_acceptance"))
    suite.addTests(loader.loadTestsFromName("tests.test_round2_acceptance"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print()
    if result.wasSuccessful():
        print("ROUND3_VERIFY: PASS (offline only; testnet NOT executed; strategy NOT validated)")
        return 0
    print("ROUND3_VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
