#!/bin/zsh
# Read-only snapshot for 48h observe. Does not change trading state.
set -euo pipefail
ROOT="/Users/zengyun/Downloads/我的AI/crypto"
LOG="$ROOT/docs/observe_48h_raw.jsonl"
STAMP=$(date '+%Y-%m-%d %H:%M:%S %z')
mkdir -p "$ROOT/docs"
python3 - <<PY >>"$LOG"
import json, urllib.request, pathlib, time
from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))
now = datetime.now(CST).isoformat()
base = "http://127.0.0.1:8787"

def get(path, timeout=12):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)}

health = get("/api/health")
status = get("/api/trading/status")
live = get("/api/live")
hist = get("/api/trading/history")
items = hist if isinstance(hist, list) else (hist.get("items") or hist.get("history") or hist.get("trades") or [])
if not isinstance(items, list):
    items = []
pos_path = pathlib.Path("/Users/zengyun/Downloads/我的AI/crypto/runtime/review/positions.json")
pos = {}
if pos_path.exists():
    try:
        pos = json.loads(pos_path.read_text())
    except Exception as e:
        pos = {"_error": str(e)}
ledger = pathlib.Path("/Users/zengyun/Downloads/我的AI/crypto/runtime/review/trade_ledger.jsonl")
hist_file = pathlib.Path("/Users/zengyun/Downloads/我的AI/crypto/runtime/review/trading_history.jsonl")
rec = {
    "ts_local": now,
    "unix": time.time(),
    "health": health,
    "trading": {
        "enabled": status.get("enabled"),
        "connected": status.get("connected"),
        "error": status.get("error"),
        "reconciliation_needed": status.get("reconciliation_needed"),
        "positions": status.get("positions"),
        "exchange_position": status.get("exchange_position"),
        "balance": status.get("balance"),
        "guardian": status.get("guardian"),
        "last_actions": status.get("last_actions"),
    },
    "live": {
        "cs": live.get("cs"),
        "decision": live.get("decision"),
        "thresholds": live.get("thresholds"),
        "mark_price": live.get("mark_price"),
        "is_full_cs": live.get("is_full_cs"),
        "faces": [
            {"name": f.get("name"), "s": f.get("s")}
            for f in (live.get("faces") or [])
        ] if isinstance(live, dict) else None,
    },
    "history_api_count": len(items),
    "history_file_lines": sum(1 for _ in hist_file.open()) if hist_file.exists() else 0,
    "ledger_file_lines": sum(1 for _ in ledger.open()) if ledger.exists() else 0,
    "positions_file": pos,
}
print(json.dumps(rec, ensure_ascii=False, default=str))
PY
echo "SNAPSHOT_OK $STAMP"
