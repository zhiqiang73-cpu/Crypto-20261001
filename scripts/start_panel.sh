#!/bin/bash
# 稳定启动面板 (macOS 无 setsid → 用 Python start_new_session)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG=/tmp/panel_v8.log
PIDFILE=/tmp/panel_v8.pid

if [[ -f "$PIDFILE" ]]; then
  old=$(cat "$PIDFILE" || true)
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "panel already running pid=$old"
    exit 0
  fi
fi

if command -v lsof >/dev/null; then
  for p in $(lsof -t -i :8787 2>/dev/null || true); do
    kill -9 "$p" 2>/dev/null || true
  done
  sleep 1
fi

: > "$LOG"
python3 - <<'PY'
import os, subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parent if False else Path.cwd()
log = open("/tmp/panel_v8.log", "a")
proc = subprocess.Popen(
    [sys.executable, "-m", "review.panel_server"],
    cwd=str(Path.cwd()),
    stdout=log,
    stderr=log,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
Path("/tmp/panel_v8.pid").write_text(str(proc.pid))
print(f"started pid={proc.pid} log=/tmp/panel_v8.log")
PY

sleep 5
curl -sS -m 8 "http://127.0.0.1:8787/api/health" || echo "warming..."
