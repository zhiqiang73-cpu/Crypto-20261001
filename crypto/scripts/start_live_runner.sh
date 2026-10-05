#!/bin/bash
# 启动【主网真实资金】交易运行器（双重确认 + 交互确认；Ctrl-C 停止）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "=============================================================="
echo "  即将以【主网真实资金】模式启动交易运行器（shadow.deploy --execute）"
echo "  下单地址: https://fapi.binance.com（真实资金）"
echo "  双重确认: TRADING_MODE=live + CONFIRM_MAINNET=YES_I_UNDERSTAND"
echo "  前提: 主网 API 凭据已在面板保存，并通过只读测试"
echo "  提示: 想先只看不下单，可去掉 --execute 手动运行一次"
echo "=============================================================="
read -r -p "确认启动实盘？输入 yes 继续: " ans
if [ "$ans" != "yes" ]; then
  echo "已取消"
  exit 1
fi
export TRADING_MODE=live
export CONFIRM_MAINNET=YES_I_UNDERSTAND
export PYTHONPATH="$ROOT"
exec python3 -m shadow.deploy --execute --interval 15
