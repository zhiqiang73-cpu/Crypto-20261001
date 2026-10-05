#!/bin/bash
# 启动「实盘版」面板 + 前端控制台（用于保存主网 API 凭据、查看状态）
# 面板: http://127.0.0.1:8787/    前端: http://127.0.0.1:8788/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "目录: $ROOT"

if command -v lsof >/dev/null 2>&1 && lsof -t -i :8788 >/dev/null 2>&1; then
  echo "前端已在运行: http://127.0.0.1:8788/"
else
  nohup python3 -m http.server 8788 --bind 127.0.0.1 --directory frontend >/tmp/rt_frontend.log 2>&1 &
  echo "前端已启动: http://127.0.0.1:8788/"
fi

if command -v lsof >/dev/null 2>&1 && lsof -t -i :8787 >/dev/null 2>&1; then
  echo "面板已在运行: http://127.0.0.1:8787/"
else
  nohup python3 -m review.panel_server >/tmp/rt_panel.log 2>&1 &
  echo "面板已启动: http://127.0.0.1:8787/"
fi

echo
echo "下一步: 打开面板 → 账户页 → 「主网 API 凭据部署」→ 填写并保存 → 点「测试」验证（只读）"
echo "然后运行 scripts/start_live_runner.sh 开始实盘（带交互确认）。"
