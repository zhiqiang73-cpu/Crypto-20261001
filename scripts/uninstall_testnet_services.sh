#!/bin/bash
# 停止并移除 scripts/install_testnet_services.sh 安装的三个本地 launchd 服务。
# 不会删除交易所订单、成交、账户余额、策略日志或 runtime/secrets.json。
set -euo pipefail

UID_NUM="$(id -u)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABELS=(
  "com.crypto.btcusdt.testnet.trader"
  "com.crypto.btcusdt.testnet.panel"
  "com.crypto.btcusdt.testnet.frontend"
  "com.crypto.btcusdt.testnet.monitor"
)

for label in "${LABELS[@]}"; do
  launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
  rm -f "$LAUNCH_DIR/$label.plist"
  echo "已移除：$label"
done

echo "本地 launchd 服务已停止并移除。交易所历史和本地运行时记录均未删除。"
