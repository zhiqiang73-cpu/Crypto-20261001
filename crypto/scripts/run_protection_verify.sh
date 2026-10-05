#!/bin/bash
# 验证生产函数自动挂止盈止损（真实资金，0.002 BTC ≈ 171 USDT）
# 用法: bash scripts/run_protection_verify.sh
# 注意：必须强制 arm64 运行——某些启动器上下文（Rosetta）会让 /usr/bin/python3
# 以 x86_64 运行，而本机 numpy 为 arm64，导入会失败。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export TRADING_MODE=live
export CONFIRM_MAINNET=YES_I_UNDERSTAND
if arch -arm64 true 2>/dev/null; then
  exec arch -arm64 /usr/bin/python3 scripts/mainnet_protection_verify.py
else
  exec /usr/bin/python3 scripts/mainnet_protection_verify.py
fi
