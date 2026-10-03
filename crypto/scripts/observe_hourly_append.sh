#!/bin/zsh
# Append hourly tick + reliability/opportunity critique (read-only).
set -euo pipefail
ROOT="/Users/zengyun/我的AI/crypto"
"$ROOT/scripts/observe_hourly_snapshot.sh" >/dev/null
python3 - <<'PY'
import json, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter
CST=timezone(timedelta(hours=8))
now=datetime.now(CST)
raw=Path('/Users/zengyun/我的AI/crypto/docs/observe_hourly_raw.jsonl')
log=Path('/Users/zengyun/我的AI/crypto/docs/observe_hourly_log.md')
audit_path=Path('/Users/zengyun/我的AI/crypto/runtime/review/decision_audit.jsonl')
lines=raw.read_text().strip().splitlines()
cur=json.loads(lines[-1])
t0=json.loads(lines[0]) if lines else cur
for ln in lines:
    try:
        o=json.loads(ln)
        if (o.get('trading') or {}).get('enabled') and not (o.get('trading') or {}).get('error'):
            t0=o
            break
    except Exception:
        pass
t=cur.get('trading') or {}
live=cur.get('live') or {}
g=t.get('guardian') or {}
ex=t.get('exchange_position') or {}
bal=t.get('balance') or {}
h=cur.get('health') or {}
faces=', '.join(f"{f.get('name')} {f.get('w')} s={f.get('s')}" for f in (live.get('faces') or []))
n_hist=cur.get('history_file_lines') or 0
n0=t0.get('history_file_lines') or 0
n_led=cur.get('ledger_file_lines') or 0
n0l=t0.get('ledger_file_lines') or 0
n_aud=cur.get('audit_file_lines') or 0
n0a=t0.get('audit_file_lines') or 0
tick_n=max(0, len(lines)-1)

# live detail for face critique
try:
    with urllib.request.urlopen('http://127.0.0.1:8787/api/live', timeout=15) as r:
        full=json.loads(r.read().decode())
except Exception as e:
    full={"_error": str(e)}
stale=full.get('staleness') or live.get('staleness') or {}
face_rows=[]
for f in (full.get('faces') or live.get('faces') or []):
    drivers=f.get('drivers') or []
    top=', '.join(f"{d.get('name')}={d.get('val')}" for d in drivers[:4])
    face_rows.append(f"| {f.get('name')} | {f.get('s')} | {f.get('foot') or '—'} | {top or '—'} |")
face_table='\n'.join(face_rows) or '| — | — | — | — |'
contrib=full.get('contrib')

# hour-window audit since previous hour
win_start=now - timedelta(hours=1)
win_ms=int(win_start.timestamp()*1000)
t0_ms=None
try:
    t0_ms=int(datetime.fromisoformat(t0.get('ts_local')).timestamp()*1000)
except Exception:
    t0_ms=int((now-timedelta(hours=24)).timestamp()*1000)
rows_h=[]; rows_all=[]
if audit_path.exists():
    with audit_path.open() as f:
        for line in f:
            try: o=json.loads(line)
            except Exception: continue
            ts=o.get('started_at_ms') or 0
            if ts>=t0_ms: rows_all.append(o)
            if ts>=win_ms: rows_h.append(o)
use=rows_h or rows_all[-120:]
css=[r.get('cs_final') for r in use if isinstance(r.get('cs_final'),(int,float))]
dec=Counter(r.get('decision_final') for r in use)
trad=Counter(bool(r.get('tradable')) for r in use)
blocks=Counter((r.get('primary_block') or '(none)').split('=')[0] for r in use)
marks=[r.get('mark_price') for r in use if r.get('mark_price') is not None]
mark_unique=len(set(marks))
stuck = (mark_unique<=2 and len(marks)>=10)
preds=[(r.get('face_scores') or {}).get('prediction') for r in use]
preds=[p for p in preds if isinstance(p,(int,float))]
std_long=sum(1 for c in css if c>=35)
std_short=sum(1 for c in css if c<=-40)
watch_long=sum(1 for c in css if c>=10)
watch_short=sum(1 for c in css if c<=-8)
cs_rng=f"[{min(css):.1f}, {max(css):.1f}]" if css else 'n/a'
pred_rng=f"[{min(preds):.1f}, {max(preds):.1f}]" if preds else 'n/a'
g_mark=g.get('last_mark')
s_mark=full.get('mark_price') or live.get('mark_price')
price_desync=''
try:
    if g_mark and s_mark and abs(float(g_mark)-float(s_mark))>30:
        price_desync=f"⚠️ 评分mark={s_mark} vs guardian={g_mark} 偏差 {abs(float(g_mark)-float(s_mark)):.1f}"
    elif stuck:
        price_desync=f"⚠️ 本小时 mark 几乎冻结（unique={mark_unique}/{len(marks)}）"
    else:
        price_desync='评分 mark 与 guardian 基本一致'
except Exception:
    price_desync='—'

miss_note='无 STANDARD 触及' if std_long==0 and std_short==0 else f'触及 STANDARD 多{std_long}/空{std_short}'
if watch_long or watch_short:
    miss_note += f'；WATCH 多{watch_long}/空{watch_short}'
if trad.get(True,0)==0 and use:
    miss_note += '；全程 tradable=false（开仓门关）'
pred_dilution=''
if preds and max(abs(p) for p in preds)>=30 and css and max(abs(c) for c in css)<15:
    pred_dilution='预测面剧烈但 CS 仍贴零 → 15% 权重稀释小时方向'

section=f'''
## T{tick_n} · {now.strftime("%Y-%m-%d %H:%M")} CST

| 项 | 值 |
|---|---|
| 健康 | ok={h.get("ok")} full_cs={h.get("full_cs")} trading_enabled={h.get("trading_enabled")} connected={h.get("trading_connected")} |
| trading | enabled={t.get("enabled")} connected={t.get("connected")} recon={t.get("reconciliation_needed")} error=`{t.get("error") or ""}` |
| 仓位 | local={t.get("positions")} · exchange={ex.get("side")} qty={ex.get("quantity")} |
| 钱包 | {bal.get("total_wallet_balance")} USDT · uPnL={bal.get("total_unrealized_pnl")} |
| CS / 决策 | **{live.get("cs")}** → **{live.get("decision")}** · contrib={contrib} |
| 门槛 / 权重 | {live.get("thresholds")} / {live.get("base_weights")} |
| 配置身份 | `{live.get("config_version")}` · ph=`{(live.get("parameters_hash") or live.get("content_hash") or "")[:16]}…` |
| mark / ATR | {s_mark} / {live.get("atr")} |
| guardian | health={g.get("health")} mark={g_mark} age={round((g.get("last_mark_age_sec") or -1),1)}s allow_new={g.get("allow_new_entries")} |
| stale(s) | binance={round(float(stale.get("binance") or -1),0)} pred={round(float(stale.get("predict_fun") or -1),1)} poly={stale.get("polymarket")} fg={round(float(stale.get("fear_greed") or -1),0)} |
| Δ自T0 | hist {n_hist-n0} · ledger {n_led-n0l} · audit {n_aud-n0a} |

### 四面可读性

| 面 | 分 | 缺项/脚注 | 顶部驱动 |
|---|---:|---|---|
{face_table}

### 可靠性 / 机会（本小时 audit n={len(use)}）

- 决策分布：{dict(dec)} · tradable：{dict(trad)} · 主阻断族：{blocks.most_common(3)}
- CS 范围：{cs_rng} · 预测面范围：{pred_rng}
- 机会判定：{miss_note}
- 价格同步：{price_desync}
- 稀释/不同步备注：{pred_dilution or "—"}
- 开平仓：history Δ={n_hist-n0}（相对 T0）

---
'''
with log.open('a') as f:
    f.write(section)
print(f'TICK_APPENDED T{tick_n} cs={live.get("cs")} decision={live.get("decision")} tradable_true={trad.get(True,0)} cs_rng={cs_rng} binance_stale={round(float(stale.get("binance") or -1),0)} miss={miss_note}')
PY
