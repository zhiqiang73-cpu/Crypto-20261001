#!/bin/bash
# 主网最小单功能测试包装器：设置双重确认环境变量后运行测试脚本。
# 用法:
#   bash scripts/run_mainnet_smoke.sh --dry   # 干跑：只做只读预检与数量计划，不下单
#   bash scripts/run_mainnet_smoke.sh         # 真实执行（真实资金，最小单位）
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export TRADING_MODE=live
export CONFIRM_MAINNET=YES_I_UNDERSTAND
if [ "${1:-}" = "--dry" ]; then
  export DRY_RUN=1
  echo "===== 主网测试 · 干跑模式（不下单）====="
else
  echo "===== 主网最小单功能测试 · 真实资金 ====="
fi

python3 scripts/mainnet_min_smoke.py
RC=$?
echo "wrapper_exit=$RC"
exit $RC
