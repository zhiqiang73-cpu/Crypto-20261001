#!/bin/bash
# 启动「实盘版」面板 + 前端控制台（用于保存主网 API 凭据、查看状态）
# 面板: http://127.0.0.1:8787/    前端: http://127.0.0.1:8788/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "目录: $ROOT"

check_port() {
  local port="$1" name="$2"
  if command -v lsof >/dev/null 2>&1 && lsof -t -i ":$port" >/dev/null 2>&1; then
    echo "[!] 端口 $port 已被占用（$name）。"
    echo "    若那是旧目录启动的面板/前端，请先运行:"
    echo "      pkill -f review.panel_server ; pkill -f 'http.server 8788'"
    echo "    然后重新运行本脚本。"
    return 1
  fi
  return 0
}

frontend_ok=1
panel_ok=1
check_port 8788 "前端" || frontend_ok=0
check_port 8787 "面板" || panel_ok=0

if [ "$frontend_ok" = 0 ] || [ "$panel_ok" = 0 ]; then
  echo
  echo "请先处理上面的端口占用，再重新运行本脚本。"
  exit 1
fi

nohup python3 -m http.server 8788 --bind 127.0.0.1 --directory frontend >/tmp/rt_frontend.log 2>&1 &
echo "前端已启动: http://127.0.0.1:8788/"

nohup python3 -m review.panel_server >/tmp/rt_panel.log 2>&1 &
sleep 1
echo "面板已启动: http://127.0.0.1:8787/"

echo
echo "下一步: 打开面板 → 账户页 → 「主网 API 凭据部署」→ 填写并保存 → 点「测试」验证（只读）"
echo "然后运行 scripts/start_live_runner.sh 开始实盘（带交互确认）。"
