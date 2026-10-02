#!/bin/bash
# 影子/部署策略的本地监控。
# 每小时跑一次: 追加一行状态到 runtime/shadow/monitor.log
# 北京时间 08:00: 额外生成一份完整结论报告 runtime/shadow/report_YYYY-MM-DD.md
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
OUT="$ROOT/runtime/shadow"
mkdir -p "$OUT"

NOW=$(date '+%Y-%m-%d %H:%M:%S')
LOG="$OUT/monitor.log"

RUNNING="no"
if pgrep -f "shadow.deploy" >/dev/null 2>&1; then RUNNING="yes"; fi

python3 - "$ROOT" "$NOW" "$RUNNING" <<'PY' >> "$LOG" 2>&1
import csv, json, os, sys
from datetime import datetime, timezone

root, now, running = sys.argv[1], sys.argv[2], sys.argv[3]
out = os.path.join(root, "runtime", "shadow")
state_p = os.path.join(out, "deployed_state.json")
trade_p = os.path.join(out, "deployed_trades.csv")

state = {}
if os.path.exists(state_p):
    try:
        state = json.load(open(state_p, encoding="utf-8"))
    except Exception:
        pass

rows = []
if os.path.exists(trade_p):
    try:
        rows = list(csv.DictReader(open(trade_p, encoding="utf-8")))
    except Exception:
        pass

opened = [r for r in rows if r["动作"].startswith("开")]
closed = [r for r in rows if r["动作"] == "反手平仓"]
halt = [r for r in rows if r["动作"] == "熔断"]
eq = rows[-1]["权益"] if rows else "?"
print(f"[{now}] running={running} 权益={eq} 开仓={len(opened)} 平仓={len(closed)} "
      f"熔断={len(halt)} halted={state.get('halted')} 最后K线={state.get('last_ts')}")
PY

# 北京时间 08:00 → 生成完整结论
if [ "$(date '+%H')" = "08" ]; then
  DAY=$(date '+%Y-%m-%d')
  python3 - "$ROOT" "$DAY" > "$OUT/report_$DAY.md" 2>&1 <<'PY'
import csv, json, os, sys
from datetime import datetime, timezone

root, day = sys.argv[1], sys.argv[2]
out = os.path.join(root, "runtime", "shadow")
state_p, trade_p = os.path.join(out, "deployed_state.json"), os.path.join(out, "deployed_trades.csv")

state = json.load(open(state_p, encoding="utf-8")) if os.path.exists(state_p) else {}
rows = list(csv.DictReader(open(trade_p, encoding="utf-8"))) if os.path.exists(trade_p) else []

def f(x):
    try:
        return float(x)
    except Exception:
        return 0.0

closed = [r for r in rows if r["动作"] == "反手平仓"]
halt = [r for r in rows if r["动作"] == "熔断"]
opens = [r for r in rows if r["动作"].startswith("开")]
last_eq = f(rows[-1]["权益"]) if rows else 0.0

print(f"# KDJ+RSI 策略监控结论 · {day} (北京时间)\n")
print(f"生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
print("## 运行状态\n")
print(f"- 进程运行中: {'是' if os.popen('pgrep -f shadow.deploy').read().strip() else '否'}")
print(f"- 已处理至 K 线时间戳: {state.get('last_ts')}")
print(f"- 熔断标志: {state.get('halted')}")
print(f"- 最近权益: {last_eq:.2f} USDT\n")
print("## 累计统计\n")
print(f"- 开仓次数: {len(opens)}")
print(f"- 平仓次数: {len(closed)}")
print(f"- 熔断次数: {len(halt)}\n")
if closed:
    wins = [r for r in closed if f(r["净盈亏"]) > 0]
    tot = sum(f(r["净盈亏"]) for r in closed)
    fee = sum(f(r["开仓手续费"]) + f(r["平仓手续费"]) for r in closed)
    print("## 成交明细\n")
    print(f"- 胜率: {len(wins)/len(closed):.1%}")
    print(f"- 累计净盈亏: {tot:+.2f} USDT")
    print(f"- 累计手续费: {fee:.2f} USDT\n")
    print("| 开仓时间 | 方向 | 开仓价 | 平仓价 | 净盈亏 | 平仓原因 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in closed[-30:]:
        print(f"| {r['开仓时间']} | {r['方向']} | {r['开仓价']} | {r['平仓价']} | "
              f"{r['净盈亏']} | {r['平仓原因']} |")
if halt:
    print("\n## 熔断事件\n")
    for r in halt:
        print(f"- {r['时间']}: {r['说明']} (权益 {r['权益']})")
print("\n## 说明\n")
print("本报告由本地 launchd 定时任务自动生成，不依赖 Manus 会话。")
print("交易进程若已停止，报告中的进程状态会显示为「否」，需人工重启。")
PY
fi

# 日志轮转: 超过 5000 行保留最后 2000 行
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
