#!/usr/bin/env python3
"""离线可分离性验证：JEV regime 打标 vs KDJ×MACD 策略绩效。

只读 repo 源码与历史 CSV；只写 outputs/regime-study-20261004/。
绝不触碰 runtime/、shadow/、config/、trading/、engine/。

用法：
    python3 scripts/regime_study/study.py --smoke          # 8 个信号，验证管线
    python3 scripts/regime_study/study.py                  # 全量（600/标的）
    python3 scripts/regime_study/study.py --no-jev         # 只跑启发式对照
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

from shadow.indicators import kdj, macd, atr_wilder, boll   # noqa: E402
from shadow.signals import entry_signal, macd_gate          # noqa: E402

KLINES_DIR = os.path.join(REPO, "inputs", "klines")
OUTDIR = os.path.join(REPO, "outputs", "regime-study-20261004")
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
SEED = 20261004
SYMBOLS = ("BTCUSDT", "ETHUSDT")
ATR_RANK_WINDOW = 2880          # 30 天 15m

_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)


# --------------------------------------------------------------------- 数据

def load_klines(symbol: str):
    path = os.path.join(KLINES_DIR, f"{symbol}_15m.csv")
    arr = np.loadtxt(path, delimiter=",", skiprows=1, usecols=(0, 1, 2, 3, 4, 5))
    return arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]


def resample_1h(o, h, lo, c):
    """15m → 1h（每 4 根一组；序列从 00:00 UTC 起，按索引分组即对齐）。"""
    n = len(c) // 4 * 4
    return (o[:n].reshape(-1, 4)[:, 0], h[:n].reshape(-1, 4).max(axis=1),
            lo[:n].reshape(-1, 4).min(axis=1), c[:n].reshape(-1, 4)[:, -1])


def indicators_for(symbol: str):
    ot, o, h, lo, c = load_klines(symbol)
    k, d, _j = kdj(h, lo, c)
    _dif, _dea, hist = macd(c)
    mb, up, lb, _sd = boll(c)

    # 1h ATR(Wilder) 对齐回 15m：bar i 用「上一根已收盘 1h」（无未来函数）
    o1, h1, l1, c1 = resample_1h(o, h, lo, c)
    atr1h = atr_wilder(h1, l1, c1, 14)
    atr_1h = np.full(len(c), np.nan)
    for i in range(len(c)):
        idx = i // 4 - 1
        if 0 <= idx < len(atr1h):
            atr_1h[i] = atr1h[idx]
    atr_pct = atr_1h / c * 100.0

    return dict(ot=ot, o=o, h=h, lo=lo, c=c, k=k, d=d, hist=hist,
                mb=mb, up=up, lb=lb, atr_pct=atr_pct)


def atr_rank_for(atr_pct, i, win=ATR_RANK_WINDOW):
    """当前 1h-ATR% 在最近 30 天的分位（按需计算，避免全序列 O(n·win)）。"""
    v = atr_pct[i]
    if not math.isfinite(v):
        return None
    a = max(0, i - win + 1)
    seg = atr_pct[a:i + 1]
    seg = seg[np.isfinite(seg)]
    if seg.size < 100:
        return None
    return float((seg <= v).mean() * 100.0)


def find_signals(ind) -> list:
    """复现 shadow/engine.py: entry_signal + macd_gate。返回 [(i, side)]，按 i 升序。"""
    k, d, hist = ind["k"], ind["d"], ind["hist"]
    out = []
    for i in range(1, len(k)):
        if not (math.isfinite(k[i]) and math.isfinite(d[i]) and math.isfinite(hist[i])):
            continue
        sl, ss, _g, _dl = entry_signal(k[i - 1], d[i - 1], k[i], d[i])
        sl, ss, _note = macd_gate(sl, ss, float(hist[i]))
        if sl:
            out.append((i, 1))
        elif ss:
            out.append((i, -1))
    return out


# --------------------------------------------------------------------- state

def _structure_text(h, lo, c, i, look=96) -> str:
    a = max(0, i - look + 1)
    sh, sl, sc = h[a:i + 1], lo[a:i + 1], c[a:i + 1]
    hi, low_, close = float(sh.max()), float(sl.min()), float(c[i])
    net = (close / float(sc[0]) - 1.0) * 100.0
    pos = (close - low_) / (hi - low_) * 100.0 if hi > low_ else 50.0
    q = max(1, len(sc) // 4)
    hh = float(sh[-q:].max()) > float(sh[:q].max())
    hl = float(sl[-q:].min()) > float(sl[:q].min())
    if hh and hl:
        shape = "higher highs and higher lows"
    elif (not hh) and (not hl):
        shape = "lower highs and lower lows"
    else:
        shape = "mixed/overlapping swings"
    return (f"Net move {net:+.1f}% over last ~24h; range {low_:.1f}-{hi:.1f}; "
            f"price at {pos:.0f}% of that range; {shape}.")


def build_state(symbol, ind, i, side) -> dict:
    c, k, d, hist = ind["c"], ind["k"], ind["d"], ind["hist"]
    close = float(c[i])

    def ret(nb):
        return round((close / float(c[i - nb]) - 1.0) * 100.0, 3) if i - nb >= 0 else None

    same = 0
    for j in range(i, 0, -1):
        if c[j] > c[j - 1]:
            if same < 0:
                break
            same += 1
        elif c[j] < c[j - 1]:
            if same > 0:
                break
            same -= 1
        else:
            break

    mb = float(ind["mb"][i]) if math.isfinite(ind["mb"][i]) else None
    bw = None
    if mb and math.isfinite(ind["up"][i]) and math.isfinite(ind["lb"][i]):
        bw = round((float(ind["up"][i]) - float(ind["lb"][i])) / mb * 100.0, 3)

    feats = {
        "close": close, "ret_1bar_pct": ret(1), "ret_8bar_pct": ret(8),
        "ret_96bar_pct": ret(96),
        "atr14_1h_pct": round(float(ind["atr_pct"][i]), 3) if math.isfinite(ind["atr_pct"][i]) else None,
        "atr_pct_30d_percentile": (lambda r: round(r, 0) if r is not None else None)(atr_rank_for(ind["atr_pct"], i)),
        "boll_width_pct": bw,
        "dist_close_to_boll_mid_pct": round((close - mb) / mb * 100.0, 3) if mb else None,
        "consecutive_same_dir_bars": same,
        "last_24h_structure": _structure_text(ind["h"], ind["lo"], c, i),
    }
    state = {
        "symbol": symbol,
        "as_of": datetime.fromtimestamp(int(ind["ot"][i]) / 1000, tz=timezone.utc)
                          .strftime("%Y-%m-%d %H:%M UTC"),
        "signal": {"side": "long" if side == 1 else "short",
                   "k": round(float(k[i]), 2), "d": round(float(d[i]), 2),
                   "macd_hist": round(float(hist[i]), 6)},
        "price_context": feats,
    }
    return state, feats


# --------------------------------------------------------------------- JEV

def _api_key() -> str:
    with open(os.path.expanduser("~/.config/typesafe/api_key"), encoding="utf-8") as fh:
        return fh.read().strip()


QUESTIONS = {
    "regime": {
        "type": "choice",
        "instructions": ("Classify the current 15m market environment for a trend-following "
                         "KDJ/MACD crossover strategy on this symbol. Base your answer only on "
                         "the described state."),
        "criteria": {
            "trend": "Sustained directional movement; pullbacks are shallow; a fresh crossover is more likely to continue than immediately reverse.",
            "range": "Sideways/choppy; price oscillating within a band; crossovers frequently whipsaw and reverse.",
            "event": "High volatility or abnormal candle behavior; large gaps, spikes, or erratic swings.",
        },
    },
    "continuation": {
        "type": "noul",
        "instructions": ("Given this state, would a fresh KDJ/MACD crossover in the signaled "
                         "direction more likely continue than immediately reverse? "
                         "Near 1 = likely to continue; near 0 = likely to reverse."),
    },
}


def jev_call(state: dict, key: str, retries: int = 4):
    body = json.dumps({"state": state, "model": MODEL, "questions": QUESTIONS}).encode()
    last = None
    for attempt in range(retries):
        t0 = time.time()
        req = urllib.request.Request(API_URL, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            return payload, time.time() - t0
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (429, 529):
                time.sleep(min(2 ** attempt, 20))
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"JEV call failed after {retries} tries: {last}")


# --------------------------------------------------------------------- 结局

def trade_outcomes(ind, signals):
    """每笔信号：t+1 开盘进场；出场 = 下一个反向信号的 t+1 开盘（无则末根收盘）。"""
    o, c = ind["o"], ind["c"]
    n = len(c)
    idxs = [i for i, _ in signals]
    sides = [s for _, s in signals]

    next_of = {1: None, -1: None}          # 从当前位置往右，最近的同向信号
    out = {}
    for p in range(len(signals) - 1, -1, -1):
        i, s = signals[p]
        e = min(i + 1, n - 1)
        entry = float(o[e])
        opp = next_of[-s]
        if opp is not None:
            xi = min(opp + 1, n - 1)
            exit_px, bars, trunc = float(o[xi]), opp - i, False
        else:
            exit_px, bars, trunc = float(c[n - 1]), n - 1 - i, True
        # whipsaw：i+1..i+8 内是否出现反向信号
        a = bisect.bisect_right(idxs, i)
        b = bisect.bisect_right(idxs, i + 8)
        whip = any(sides[k] != s for k in range(a, b))
        out[i] = dict(
            trade_ret=s * (exit_px / entry - 1.0) * 100.0,
            fwd_ret_8=s * (float(c[min(i + 8, n - 1)]) / entry - 1.0) * 100.0,
            fwd_ret_32=s * (float(c[min(i + 32, n - 1)]) / entry - 1.0) * 100.0,
            bars_held=bars, truncated=trunc, whipsaw=whip)
        next_of[s] = i
    return out


# --------------------------------------------------------------------- 启发式对照

def heuristic_regime(feat):
    rank = feat.get("atr_pct_30d_percentile")
    atr = feat.get("atr14_1h_pct")
    r96 = feat.get("ret_96bar_pct")
    if rank is not None and rank >= 80:
        return "event"
    if atr and r96 is not None and atr > 0 and abs(r96) / atr > 2.5:
        return "trend"
    return "range"


# --------------------------------------------------------------------- 统计

def perm_test(arrays, nperm=10000, seed=SEED):
    arrays = [np.asarray(a, float) for a in arrays if len(a) > 0]
    if len(arrays) < 2:
        return None, None
    means = [a.mean() for a in arrays]
    obs = max(means) - min(means)
    pooled = np.concatenate(arrays)
    sizes = [len(a) for a in arrays]
    rng = np.random.default_rng(seed)
    ge = 0
    for _ in range(nperm):
        p = rng.permutation(pooled)
        s, ss = 0, []
        for sz in sizes:
            ss.append(p[s:s + sz].mean())
            s += sz
        if (max(ss) - min(ss)) >= obs:
            ge += 1
    return float(obs), ge / nperm


def summarize(rows, key, value="trade_return_pct"):
    groups = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r[value])
    out = {}
    for g, vals in sorted(groups.items()):
        a = np.asarray(vals, float)
        out[g] = dict(n=len(a), mean=round(float(a.mean()), 4),
                      median=round(float(np.median(a)), 4),
                      std=round(float(a.std(ddof=1)), 4) if len(a) > 1 else 0.0,
                      winrate=round(float((a > 0).mean()), 3))
    obs, p = perm_test([np.asarray(v, float) for v in groups.values()])
    return out, obs, p


# --------------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    if args.smoke:
        args.limit, args.concurrency = 8, 2
    os.makedirs(OUTDIR, exist_ok=True)
    key = None if args.no_jev else _api_key()

    inds, all_sigs = {}, {}
    for symbol in SYMBOLS:
        inds[symbol] = indicators_for(symbol)
        all_sigs[symbol] = find_signals(inds[symbol])
        log(f"{symbol}: {len(inds[symbol]['c'])} bars, {len(all_sigs[symbol])} 有效开仓信号")

    rng = np.random.default_rng(SEED)
    samples = []          # (symbol, i, side, state, feats)
    for symbol in SYMBOLS:
        sigs = sorted(all_sigs[symbol])
        if not sigs:
            continue
        take = min(args.limit, len(sigs))
        bins = np.array_split(np.arange(len(sigs)), take) if take else []
        chosen = sorted(int(seg[rng.integers(len(seg))]) for seg in bins)
        for ci in chosen:
            i, side = sigs[ci]
            state, feats = build_state(symbol, inds[symbol], i, side)
            samples.append((symbol, i, side, state, feats))
    log(f"采样 {len(samples)} 个信号")

    results, latencies = {}, []
    if not args.no_jev and samples:
        lock = threading.Lock()
        done = [0]

        def work(item):
            symbol, i, side, state, feats = item
            payload, lat = jev_call(state, key)
            ans = payload["answers"]
            with lock:
                latencies.append(lat)
                done[0] += 1
                if done[0] % 25 == 0:
                    log(f"JEV 进度 {done[0]}/{len(samples)} (最近 {lat:.2f}s)")
            return (symbol, i), {
                "regime": ans["regime"]["choice"],
                "regime_probs": json.dumps(ans["regime"]["probabilities"]),
                "regime_conf": ans["regime"]["confidence"],
                "continuation": ans["continuation"]["noul"]}

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            for f in as_completed([ex.submit(work, it) for it in samples]):
                k, v = f.result()
                results[k] = v
        log(f"JEV 完成 {len(results)} 条，用时 {time.time() - t0:.0f}s")

    outcomes = {s: trade_outcomes(inds[s], all_sigs[s]) for s in SYMBOLS if all_sigs[s]}

    rows = []
    for symbol, i, side, state, feats in samples:
        oc = outcomes[symbol][i]
        r = {"symbol": symbol, "bar_utc": state["as_of"],
             "side": "long" if side == 1 else "short",
             "heuristic_regime": heuristic_regime(feats),
             "trade_return_pct": round(oc["trade_ret"], 4),
             "fwd_ret_8": round(oc["fwd_ret_8"], 4),
             "fwd_ret_32": round(oc["fwd_ret_32"], 4),
             "bars_held": oc["bars_held"], "whipsaw": oc["whipsaw"],
             "truncated": oc["truncated"]}
        r.update(results.get((symbol, i), {}))
        rows.append(r)

    cols = ["symbol", "bar_utc", "side", "regime", "regime_probs", "regime_conf",
            "continuation", "heuristic_regime", "trade_return_pct", "fwd_ret_8",
            "fwd_ret_32", "bars_held", "whipsaw", "truncated"]
    with open(os.path.join(OUTDIR, "labels.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log("写出 labels.csv")

    analysis = {}
    jrows = [r for r in rows if "regime" in r]
    if jrows:
        g, obs, p = summarize(jrows, "regime")
        analysis["jev_regime"] = {"groups": g, "spread": obs, "perm_p": p}
        base = float(np.mean([r["trade_return_pct"] for r in jrows]))
        gated = [r["trade_return_pct"] for r in jrows if r["regime"] != "range"]
        analysis["gate_skip_range"] = {
            "baseline_mean": round(base, 4), "baseline_n": len(jrows),
            "gated_mean": round(float(np.mean(gated)), 4) if gated else None,
            "gated_n": len(gated)}
        for th in (0.5, 0.6, 0.7):
            sub = [r["trade_return_pct"] for r in jrows if r["continuation"] >= th]
            analysis[f"cont_ge_{th}"] = {
                "n": len(sub), "mean": round(float(np.mean(sub)), 4) if sub else None}
    hg, hobs, hp = summarize(rows, "heuristic_regime")
    analysis["heuristic_regime"] = {"groups": hg, "spread": hobs, "perm_p": hp}
    analysis["sample_size"] = len(rows)
    with open(os.path.join(OUTDIR, "analysis.json"), "w", encoding="utf-8") as fh:
        json.dump(analysis, fh, ensure_ascii=False, indent=2)

    if latencies:
        lat = np.asarray(latencies)
        with open(os.path.join(OUTDIR, "latency.json"), "w", encoding="utf-8") as fh:
            json.dump({"n": len(lat), "p50": round(float(np.percentile(lat, 50)), 3),
                       "p95": round(float(np.percentile(lat, 95)), 3),
                       "max": round(float(lat.max()), 3)}, fh, indent=2)

    if not args.no_jev and len(samples) >= 8:
        stab = []
        picks = [samples[int(x)] for x in np.linspace(0, len(samples) - 1, 8).astype(int)]
        for symbol, i, side, state, feats in picks:
            choices, conts = [], []
            for _ in range(5):
                payload, _ = jev_call(state, key)
                choices.append(payload["answers"]["regime"]["choice"])
                conts.append(payload["answers"]["continuation"]["noul"])
            stab.append({"symbol": symbol, "as_of": state["as_of"], "choices": choices,
                         "agree": choices.count(max(set(choices), key=choices.count)) / 5,
                         "cont_std": round(float(np.std(conts)), 3)})
        with open(os.path.join(OUTDIR, "stability.json"), "w", encoding="utf-8") as fh:
            json.dump(stab, fh, ensure_ascii=False, indent=2)
        log("稳定性完成")

    lines = ["# Regime 可分离性验证（JEV 打标 vs KDJ×MACD）", "",
             f"- 采样信号：{len(rows)}"]
    if latencies:
        lat = np.asarray(latencies)
        lines.append(f"- JEV 延迟：p50 {np.percentile(lat,50):.2f}s / p95 {np.percentile(lat,95):.2f}s")
    if "jev_regime" in analysis:
        lines += ["", "## JEV regime 分组（trade_return_pct）", "",
                  "| regime | n | 平均% | 中位% | 胜率 |", "|---|---|---|---|---|"]
        for gname, v in analysis["jev_regime"]["groups"].items():
            lines.append(f"| {gname} | {v['n']} | {v['mean']} | {v['median']} | {v['winrate']} |")
        lines += ["", f"- 组间极差 {analysis['jev_regime']['spread']}，置换检验 p = {analysis['jev_regime']['perm_p']}"]
        a = analysis["gate_skip_range"]
        lines.append(f"- 门控(跳过 range)：基线 {a['baseline_mean']}% (n={a['baseline_n']}) → "
                     f"门控后 {a['gated_mean']}% (n={a['gated_n']})")
        for th in (0.5, 0.6, 0.7):
            kk = f"cont_ge_{th}"
            lines.append(f"- continuation ≥ {th}：n={analysis[kk]['n']}，均值 {analysis[kk]['mean']}%")
    lines += ["", "## 启发式对照（纯代码 regime）", "",
              "| regime | n | 平均% | 中位% | 胜率 |", "|---|---|---|---|---|"]
    for gname, v in analysis["heuristic_regime"]["groups"].items():
        lines.append(f"| {gname} | {v['n']} | {v['mean']} | {v['median']} | {v['winrate']} |")
    lines += ["", f"- 组间极差 {analysis['heuristic_regime']['spread']}，置换检验 p = {analysis['heuristic_regime']['perm_p']}"]
    with open(os.path.join(OUTDIR, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    log("完成。")
    print("\n--- 摘要 ---")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
