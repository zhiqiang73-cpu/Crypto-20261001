#!/bin/bash
# 由 launchd 启动的每小时监控循环。不提交委托，只记录状态/日报。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

while true; do
  bash scripts/monitor_shadow.sh || true
  sleep 3600
done
