#!/bin/zsh
# 30-min observe: score sanity/stability, fills, money-path blockers, doubts (read-only).
set -euo pipefail
ROOT="/Users/zengyun/我的AI/crypto"
RAW="$ROOT/docs/observe_30m_raw.jsonl"
LOG="$ROOT/docs/observe_30m_log.md"
DOUBTS="$ROOT/docs/observe_doubts.md"
AUDIT="$ROOT/docs/../runtime/review/decision_audit.jsonl"
AUDIT="/Users/zengyun/我的AI/crypto/runtime/review/decision_audit.jsonl"
HIST="/Users/zengyun/我的AI/crypto/runtime/review/trading_history.jsonl"
LEDGER="/Users/zengyun/我的AI/crypto/runtime/review/trade_ledger.jsonl"
mkdir -p "$ROOT/docs"

# reuse snapshot into 30m raw
python3 - <<'PY' >>"$RAW"
import json, urllib.request, pathlib, time
from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))
now = datetime.now(CST).isoformat()
base = "http://127.0.0.1:8787"

def get(path, timeout=15):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)}

health = get("/api/health")
status = get("/api/trading/status")
live = get("/api/live")
live_long = get("/api/live?horizon=long")
hist = get("/api/trading/history")
items = hist if isinstance(hist, list) else (hist.get("items") or hist.get("history") or hist.get("trades") or [])
if not isinstance(items, list):
    items = []
pos_path = pathlib.Path("/Users/zengyun/我的AI/crypto/runtime/review/positions.json")
pos = {}
if pos_path.exists():
    try:
        pos = json.loads(pos_path.read_text())
    except Exception as e:
        pos = {"_error": str(e)}
ledger = pathlib.Path("/Users/zengyun/我的AI/crypto/runtime/review/trade_ledger.jsonl")
hist_file = pathlib.Path("/Users/zengyun/我的AI/crypto/runtime/review/trading_history.jsonl")
audit = pathlib.Path("/Users/zengyun/我的AI/crypto/runtime/review/decision_audit.jsonl")
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
        "leverage": status.get("leverage"),
        "last_actions": status.get("last_actions"),
    },
    "live": {
        "cs": live.get("cs"),
        "decision": live.get("decision"),
        "thresholds": live.get("thresholds"),
        "base_weights": live.get("base_weights"),
        "config_version": live.get("config_version"),
        "parameters_hash": live.get("parameters_hash") or live.get("content_hash"),
        "mark_price": live.get("mark_price"),
        "atr": live.get("atr"),
        "faces": [{"name": f.get("name"), "w": f.get("w"), "s": f.get("s"), "foot": f.get("foot")} for f in (live.get("faces") or [])],
        "contrib": live.get("contrib"),
        "staleness": live.get("staleness"),
        "note": live.get("note"),
    },
    "live_long": {
        "cs": live_long.get("cs"),
        "decision": live_long.get("decision"),
        "faces": [{"name": f.get("name"), "s": f.get("s")} for f in (live_long.get("faces") or [])],
    },
    "history_api_count": len(items),
    "history_file_lines": sum(1 for _ in hist_file.open()) if hist_file.exists() else 0,
    "ledger_file_lines": sum(1 for _ in ledger.open()) if ledger.exists() else 0,
    "audit_file_lines": sum(1 for _ in audit.open()) if audit.exists() else 0,
    "positions_file": pos,
}
print(json.dumps(rec, ensure_ascii=False, default=str))
PY

python3 - <<'PY'
import json, math, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter
CST=timezone(timedelta(hours=8))
now=datetime.now(CST)
ROOT=Path('/Users/zengyun/我的AI/crypto')
raw_path=ROOT/'docs'/'observe_30m_raw.jsonl'
log_path=ROOT/'docs'/'observe_30m_log.md'
doubts_path=ROOT/'docs'/'observe_doubts.md'
audit_path=ROOT/'runtime'/'review'/'decision_audit.jsonl'
hist_path=ROOT/'runtime'/'review'/'trading_history.jsonl'
ledger_path=ROOT/'runtime'/'review'/'trade_ledger.jsonl'

lines=[json.loads(x) for x in raw_path.read_text().strip().splitlines()]
cur=lines[-1]
t0=lines[0]
for o in lines:
    t=o.get('trading') or {}
    if t.get('enabled') and not t.get('error'):
        t0=o; break
tick_n=len(lines)  # T1.. after first is baseline+ticks; use len as tick id
t=cur.get('trading') or {}
live=cur.get('live') or {}
ll=cur.get('live_long') or {}
g=t.get('guardian') or {}
ex=t.get('exchange_position') or {}
bal=t.get('balance') or {}
stale=live.get('staleness') or {}
faces=live.get('faces') or []

# 30m window audits
win_ms=int((now-timedelta(minutes=30)).timestamp()*1000)
# session start from first raw
try:
    sess_ms=int(datetime.fromisoformat(t0['ts_local']).timestamp()*1000)
except Exception:
    sess_ms=win_ms
rows=[]
if audit_path.exists():
    with audit_path.open() as f:
        for line in f:
            try: o=json.loads(line)
            except Exception: continue
            if (o.get('started_at_ms') or 0) >= win_ms:
                rows.append(o)

css=[r.get('cs_final') for r in rows if isinstance(r.get('cs_final'),(int,float))]
dec=Counter(r.get('decision_final') for r in rows)
trad=Counter(bool(r.get('tradable')) for r in rows)
blocks=Counter((r.get('primary_block') or '(none)').split('=')[0] for r in rows)
marks=[r.get('mark_price') for r in rows if isinstance(r.get('mark_price'),(int,float))]
mark_unique=len(set(round(m,1) for m in marks)) if marks else 0
face_series={k:[] for k in ('news','data','tech','prediction')}
for r in rows:
    fs=r.get('face_scores') or {}
    for k in face_series:
        v=fs.get(k)
        if isinstance(v,(int,float)): face_series[k].append(v)

def stab(vs):
    if len(vs)<2: return 'n/a', None, None
    mn, mx = min(vs), max(vs)
    sd=statistics.pstdev(vs)
    return f"[{mn:.1f},{mx:.1f}] σ={sd:.1f}", mn, mx

cs_stab, cs_mn, cs_mx = stab(css) if css else ('n/a', None, None)
std_long=sum(1 for c in css if c>=35)
std_short=sum(1 for c in css if c<=-40)
watch_long=sum(1 for c in css if c>=10)
watch_short=sum(1 for c in css if c<=-8)

# fills in window from history file
def count_new(path, since_iso):
    n=0; samples=[]
    if not path.exists(): return 0, samples
    try:
        since=datetime.fromisoformat(since_iso).timestamp()
    except Exception:
        since=now.timestamp()-1800
    for line in path.read_text().strip().splitlines()[-200:]:
        try: o=json.loads(line)
        except Exception: continue
        ts=o.get('ts') or o.get('filled_at') or o.get('time') or o.get('opened_at_ms')
        ut=None
        if isinstance(ts,(int,float)):
            ut=ts/1000 if ts>1e12 else float(ts)
        elif isinstance(ts,str):
            try: ut=datetime.fromisoformat(ts.replace('Z','+00:00')).timestamp()
            except Exception: pass
        if ut and ut >= (now.timestamp()-1800):
            n+=1
            samples.append(o)
    return n, samples

# approx: line count delta vs previous raw
prev = lines[-2] if len(lines)>=2 else t0
hist_delta=(cur.get('history_file_lines') or 0)-(prev.get('history_file_lines') or 0)
led_delta=(cur.get('ledger_file_lines') or 0)-(prev.get('ledger_file_lines') or 0)
hist_sess=(cur.get('history_file_lines') or 0)-(t0.get('history_file_lines') or 0)
wallet0=(t0.get('trading') or {}).get('balance',{}).get('total_wallet_balance')
wallet1=bal.get('total_wallet_balance')
pnl_sess=None
try:
    if wallet0 is not None and wallet1 is not None:
        pnl_sess=float(wallet1)-float(wallet0)
except Exception:
    pass

# money-path verdict
blockers=[]
if not t.get('enabled'): blockers.append('trading未开启')
if t.get('error'): blockers.append(f"trading.error={t.get('error')}")
if trad.get(True,0)==0 and rows: blockers.append('本窗全程 tradable=false')
if mark_unique<=2 and len(marks)>=20: blockers.append(f'评分mark几乎冻结 unique={mark_unique}/{len(marks)}')
try:
    if g.get('last_mark') and live.get('mark_price') and abs(float(g['last_mark'])-float(live['mark_price']))>50:
        blockers.append(f"双价格分裂 guardian={g.get('last_mark')} score={live.get('mark_price')}")
except Exception:
    pass
if std_long==0 and std_short==0:
    blockers.append('本窗未触及 STANDARD±35（无标准开仓信号）')

# doubts to append
new_doubts=[]
if float(stale.get('binance') or 0) > 60:
    new_doubts.append(('P0', 'Binance评分源持续stale，标的价冻结却仍在打分——分数时效性存疑，赚钱链路实质断开'))
if trad.get(True,0)==0 and t.get('enabled'):
    new_doubts.append(('P0', 'enabled=true 但 tradable=false：状态看起来能交易，执行层不能赚钱'))
if faces:
    pred=next((f for f in faces if '预测' in str(f.get('name'))), None)
    tech=next((f for f in faces if '技术' in str(f.get('name'))), None)
    data=next((f for f in faces if '数据' in str(f.get('name'))), None)
    try:
        if pred and abs(float(pred.get('s') or 0))>30 and abs(float(live.get('cs') or 0))<10:
            new_doubts.append(('P1', f"预测面={pred.get('s')} 剧烈但CS={live.get('cs')}贴零：15%权重是否导致小时机会无法变现？"))
        if tech and data and abs(float(tech.get('s') or 0)-float(data.get('s') or 0))>40:
            new_doubts.append(('P2', f"技术={tech.get('s')} 与 数据={data.get('s')} 大幅背离，合成CS是否掩盖矛盾？"))
        for f in faces:
            foot=str(f.get('foot') or '')
            if '缺项' in foot:
                new_doubts.append(('P2', f"{f.get('name')} 缺项：{foot} —— 客观性不足时仍参与加权？"))
    except Exception:
        pass
# face instability
for k,vs in face_series.items():
    label,_,_ = stab(vs)
    if len(vs)>=10:
        span=max(vs)-min(vs)
        if span>50:
            new_doubts.append(('P1', f"本窗{k}面振幅过大 {label}：半小时内是否噪声多于信号？"))

# score reasonableness brief
reason_bits=[]
if css:
    reason_bits.append(f"CS {cs_stab}；STANDARD多{std_long}/空{std_short}；WATCH多{watch_long}/空{watch_short}")
else:
    reason_bits.append('本窗无audit样本')
reason_bits.append(f"决策 {dict(dec)} · tradable True={trad.get(True,0)}/{len(rows)}")
face_stab_lines=[]
for k,vs in face_series.items():
    label,_,_=stab(vs)
    face_stab_lines.append(f"{k}:{label}")
money=f"会话钱包Δ={pnl_sess if pnl_sess is not None else 'n/a'} USDT · 本窗historyΔ={hist_delta} ledgerΔ={led_delta} · 会话成交行Δ={hist_sess}"
if pnl_sess==0 and hist_sess==0:
    money_verdict='本会话尚未产生可核验盈亏（无成交）——「能不能赚钱」尚无法用结果证明，只能看链路是否具备赚钱条件'
elif pnl_sess is not None and pnl_sess!=0:
    money_verdict=f"钱包已变动 {pnl_sess:+.4f} USDT（需核对是否交易盈亏）"
else:
    money_verdict='盈亏待核验'

face_now=', '.join(f"{f.get('name')}={f.get('s')}" for f in faces)
section=f'''
## T{tick_n} · {now.strftime("%Y-%m-%d %H:%M")} CST · 半小时赚钱向观察

### 核心：能不能赚钱
- **裁决**：{money_verdict}
- 链路阻断：{('; '.join(blockers) if blockers else '未见硬阻断')}
- {money}
- 仓位：exchange={ex.get('side')} qty={ex.get('quantity')} · local={t.get('positions')}
- trading：enabled={t.get('enabled')} error=`{t.get('error') or ''}` · guardian allow_new={g.get('allow_new_entries')}

### 分数是否合理 / 稳定（近30分钟）
- {'; '.join(reason_bits)}
- 四面振幅：{' · '.join(face_stab_lines) if face_stab_lines else 'n/a'}
- 此刻 short CS **{live.get('cs')} → {live.get('decision')}** · faces {face_now} · contrib={live.get('contrib')}
- long CS **{ll.get('cs')} → {ll.get('decision')}** · faces {ll.get('faces')}
- mark 评分={live.get('mark_price')} / guardian={g.get('last_mark')} · binance_stale={round(float(stale.get('binance') or -1),0)}s · pred_stale={round(float(stale.get('predict_fun') or -1),1)}s
- 配置：`{live.get('config_version')}` 权重={live.get('base_weights')} 门槛={live.get('thresholds')}

### 成交
- 本窗相对上一拍：history +{hist_delta} · ledger +{led_delta}
- last_actions：{t.get('last_actions') or []}

### 本拍新疑问
{chr(10).join('- **'+d[0]+'** '+d[1] for d in new_doubts) if new_doubts else '- （本拍无新增）'}

---
'''
if not log_path.exists():
    header=f'''# BTC 四面 · 半小时赚钱向观察

- **改档**：{now.strftime("%Y-%m-%d %H:%M")} CST 起改为 **每 30 分钟**
- **焦点**：分数合理/稳定 · 有无成交 · 系统能不能赚钱 · 疑问入库
- **原始**：`docs/observe_30m_raw.jsonl` · **疑问簿**：`docs/observe_doubts.md`
- 只读，不改代码/ACTIVE/仓位

---
'''
    log_path.write_text(header)
with log_path.open('a') as f:
    f.write(section)

# doubts ledger (dedupe by text)
if not doubts_path.exists():
    doubts_path.write_text(f'''# 观察疑问簿（赚钱目标）

> 半小时巡检自动追加；人工可继续批注。不宣称已验证 edge。

| 首次记录 | 级别 | 疑问 | 状态 |
|---|---|---|---|
''')
existing=doubts_path.read_text() if doubts_path.exists() else ''
with doubts_path.open('a') as f:
    for lvl, text in new_doubts:
        if text in existing:
            continue
        f.write(f"| {now.strftime('%m-%d %H:%M')} | {lvl} | {text} | open |\n")
        existing += text

print(json.dumps({
  'tick': tick_n,
  'cs': live.get('cs'),
  'decision': live.get('decision'),
  'tradable_true': trad.get(True,0),
  'n_audit': len(rows),
  'cs_stab': cs_stab,
  'hist_delta': hist_delta,
  'pnl_sess': pnl_sess,
  'blockers': blockers,
  'new_doubts': len(new_doubts),
  'binance_stale': round(float(stale.get('binance') or -1),0),
}, ensure_ascii=False))
PY
