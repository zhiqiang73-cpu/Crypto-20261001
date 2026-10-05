#!/bin/bash
# 启动「实盘版」面板（含主网凭据保存/测试入口；面板自带控制台 UI）
# 面板: http://127.0.0.1:8787/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "目录: $ROOT"

if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -t -i :8787 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    echo "[!] 端口 8787 已被占用（PID: $OLD_PIDS），通常是旧目录启动的面板。"
    read -r -p "是否停止旧面板并启动新面板？[y/N] " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      kill $OLD_PIDS 2>/dev/null || true
      sleep 1
    else
      echo "已取消。也可以手动执行: pkill -f review.panel_server 后重新运行本脚本。"
      exit 1
    fi
  fi
fi

nohup python3 -m review.panel_server >/tmp/rt_panel.log 2>&1 &
sleep 1
echo "面板已启动: http://127.0.0.1:8787/"

echo
echo "下一步: 打开面板 → 账户页 → 「主网 API 凭据部署」→ 填写并保存 → 点「测试」验证（只读）"
echo "然后运行 scripts/start_live_runner.sh 开始实盘（带交互确认）。"
