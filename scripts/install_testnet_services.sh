#!/bin/bash
# 安装本地 macOS launchd 服务：BTCUSDT 测试网交易器 + 面板 + 前端。
#
# 这是「生成和安装服务」脚本，不含 API Key/Secret；密钥仍只从
# runtime/secrets.json（gitignore）读取。
#
# 必须由用户在自己的 macOS 终端手动执行：
#   ENABLE_TESTNET_EXECUTION=YES bash scripts/install_testnet_services.sh
#
# 为什么强制这个环境变量？因为该命令会启动测试网的自动委托服务。脚本不能被
# 双击或误执行后无声地开始下单。
#
# 可逆：bash scripts/uninstall_testnet_services.sh
set -euo pipefail

if [[ "${ENABLE_TESTNET_EXECUTION:-}" != "YES" ]]; then
  cat >&2 <<'MSG'
拒绝安装：该脚本将启动 Binance Futures Testnet 的自动下单运行器。
若你已在本机终端确认要启用测试网自动执行，请运行：

  ENABLE_TESTNET_EXECUTION=YES bash scripts/install_testnet_services.sh

主网不会被此脚本启用：运行器仍受 TRADING_MODE=testnet 和 URL 一致性闸门保护。
MSG
  exit 64
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "找不到可执行 python3，未安装任何服务。" >&2
  exit 1
fi

if [[ ! -f "$ROOT/runtime/secrets.json" ]]; then
  echo "缺少 $ROOT/runtime/secrets.json；未安装任何服务。" >&2
  exit 1
fi

# 先做纯代码/配置预检：不调用交易所、不发任何委托。
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - <<PY
import os, sys
root = r'''$ROOT'''
sys.path.insert(0, root)
from trading.runtime_mode import validate_exchange_target
from config.market_endpoints import resolve_for_account, MARKET_TESTNET
endpoint = resolve_for_account()
validate_exchange_target(endpoint.account_base_url)
if endpoint.market != MARKET_TESTNET:
    raise SystemExit(f"拒绝安装：账户与行情不是测试网: {endpoint.describe()}")
print("预检通过：", endpoint.describe())
PY

UID_NUM="$(id -u)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$ROOT/runtime/logs"
mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

# 生成 plist。plistlib 会处理包含中文和空格的本机路径，不依赖手写 XML 转义。
"$PYTHON_BIN" - "$LAUNCH_DIR" "$ROOT" "$PYTHON_BIN" "$LOG_DIR" <<'PY'
import os
import plistlib
import sys

launch_dir, root, python_bin, log_dir = sys.argv[1:]
common = {
    "WorkingDirectory": root,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "EnvironmentVariables": {
        "TRADING_MODE": "testnet",
        "PYTHONPATH": root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    },
}
services = {
    "com.crypto.btcusdt.testnet.trader": [
        python_bin, "-m", "shadow.deploy", "--execute", "--interval", "15"
    ],
    "com.crypto.btcusdt.testnet.panel": [python_bin, "-m", "review.panel_server"],
    "com.crypto.btcusdt.testnet.frontend": [
        python_bin, "-m", "http.server", "8788", "--bind", "127.0.0.1",
        "--directory", os.path.join(root, "frontend"),
    ],
    "com.crypto.btcusdt.testnet.monitor": [
        "/bin/bash", os.path.join(root, "scripts", "run_testnet_monitor_loop.sh"),
    ],
}
for label, arguments in services.items():
    data = dict(common)
    data.update({
        "Label": label,
        "ProgramArguments": arguments,
        "StandardOutPath": os.path.join(log_dir, label + ".out.log"),
        "StandardErrorPath": os.path.join(log_dir, label + ".err.log"),
    })
    path = os.path.join(launch_dir, label + ".plist")
    with open(path, "wb") as fh:
        plistlib.dump(data, fh, sort_keys=False)
    print(path)
PY

LABELS=(
  "com.crypto.btcusdt.testnet.trader"
  "com.crypto.btcusdt.testnet.panel"
  "com.crypto.btcusdt.testnet.frontend"
  "com.crypto.btcusdt.testnet.monitor"
)

for label in "${LABELS[@]}"; do
  plist="$LAUNCH_DIR/$label.plist"
  plutil -lint "$plist" >/dev/null
  # 旧服务可能来自前一次安装；先平稳卸载再加载同名服务。
  launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID_NUM" "$plist"
  launchctl kickstart -k "gui/$UID_NUM/$label"
done

sleep 3
printf '\n已安装并启动本地测试网服务：\n'
for label in "${LABELS[@]}"; do
  printf '  - %s: ' "$label"
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    echo "已加载"
  else
    echo "未确认（查看 $LOG_DIR/$label.err.log）"
  fi
done

cat <<MSG

打开面板： http://127.0.0.1:8788/?v=16
停止并移除服务： bash scripts/uninstall_testnet_services.sh
查看交易器日志： tail -f "$LOG_DIR/com.crypto.btcusdt.testnet.trader.out.log"

交易器启动会先拒绝以下危险状态：非测试网、行情/账户市场不一致、遗留挂单、
有仓时账户不是单向/逐仓/10x。只有全部通过后，才按「已收盘K线金叉做多、死叉
做空，下一根开盘开始 post-only 限价追价；180秒后限价穿盘口兜底」运行。
MSG
