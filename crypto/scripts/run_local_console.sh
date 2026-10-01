#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export TRADING_MODE="${TRADING_MODE:-paper}"
export PYTHONDONTWRITEBYTECODE=1
cleanup(){
  [ -n "${BACK_PID:-}" ] && kill "$BACK_PID" 2>/dev/null || true
  [ -n "${FRONT_PID:-}" ] && kill "$FRONT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT
printf '%s\n' "BTC Quant Console (local)"
printf '%s\n' "Frontend: http://127.0.0.1:8788/"
printf '%s\n' "Backend:  http://127.0.0.1:8787/"
printf '%s\n' "Mode:     $TRADING_MODE"
python3 -m review.panel_server & BACK_PID=$!
python3 -m http.server 8788 --bind 127.0.0.1 --directory frontend & FRONT_PID=$!
wait -n "$BACK_PID" "$FRONT_PID"
