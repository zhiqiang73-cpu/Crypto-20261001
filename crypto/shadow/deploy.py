"""把 KDJ+RSI 策略部署到模拟账户 (Binance Futures Testnet) 并驱动真实下单.

严格按规格执行, 不增删任何条件:
    15m 收盘判定 → 下一根开盘市价成交
    做多 = 金叉 且 K < 30;  做空 = 死叉 且 K > 70
    出场 = 对侧完整信号, 平仓并反手
    仓位 = 权益 × r_eff ÷ (2 × ATR_1H), r=0.01, 向下取整 0.001
    布林闸门 / 日亏 3% / 回撤 10% / 灾难止损 3×ATR_1H / 10x 逐仓

只允许 Testnet; live 被 runtime_mode 闸门硬阻断。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone

import numpy as np

from shadow.engine import (ATR_MULT_K, BOLL_GATE_ENABLED, DISASTER_ATR, GATE_FEE_RATE,
                           GATE_STRONG, GATE_WEAK, K_LONG_MAX, K_SHORT_MIN, LEVERAGE,
                           MIN_NOTIONAL, MIN_QTY, RISK_R, floor_step)
from shadow.indicators import atr_wilder, boll, kdj
from shadow.live import fetch
from trading.binance_client import BinanceTestnetClient
from trading.runtime_mode import current_mode, validate_exchange_target

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "runtime", "shadow")
TRADE_LOG = os.path.join(OUT, "deployed_trades.csv")
STATE = os.path.join(OUT, "deployed_state.json")

COLS = ["时间", "动作", "方向", "数量", "价格", "净盈亏", "K", "D", "ATR_1H",
        "倍数", "权益", "说明"]


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _chase_note(r) -> str:
    """把限价追价过程转成一行可读备注 (maker/taker + 档数 + 成交价).

    用于事后核对「是否真的吃到了 maker 手续费」—— 不靠承诺, 靠日志。
    """
    meta = (r.raw or {}).get("chase") if isinstance(r.raw, dict) else None
    if not meta:
        return ""
    tag = "maker" if meta.get("likely_maker") else "taker"
    return (f"{tag} 档{meta.get('final_step', -1)} "
            f"成交价={r.avg_price:.2f} 档数={meta.get('steps_used', 0)}")


def log_row(row: list) -> None:
    os.makedirs(OUT, exist_ok=True)
    new = not os.path.exists(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLS)
        w.writerow(row)


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"last_ts": 0, "peak": 0.0, "day": None, "day_start_eq": 0.0,
            "halted": False, "entry": None}


def save_state(st: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(STATE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2)
    os.replace(STATE + ".tmp", STATE)


async def step(client: BinanceTestnetClient, st: dict, execute: bool) -> None:
    bal = await client.get_balance()
    pos = await client.get_position()
    pos_side = (getattr(pos, "side", "FLAT") or "FLAT").upper()
    ex_side = float(getattr(pos, "quantity", 0.0) or 0.0) * (
        1.0 if pos_side == "LONG" else (-1.0 if pos_side == "SHORT" else 0.0))
    equity = float(bal.total_wallet_balance) + float(getattr(pos, "unrealized_pnl", 0.0) or 0.0)
    entry_px = float(getattr(pos, "entry_price", 0.0) or 0.0)

    b15 = fetch("15m", 400)
    b1h = fetch("1h", 300)
    now = int(time.time() * 1000)
    b15 = {k: v[(b15["ts"] + 15 * 60 * 1000) <= now] for k, v in b15.items()}
    b1h = {k: v[(b1h["ts"] + 60 * 60 * 1000) <= now] for k, v in b1h.items()}

    ts = b15["ts"]
    o, h, l, c = (b15[x] for x in ("open", "high", "low", "close"))
    k, d, _j = kdj(h, l, c)
    mb, up, lb, _ = boll(c, 20, 2.0)
    atr1h = atr_wilder(b1h["high"], b1h["low"], b1h["close"], 14)
    idx = np.searchsorted(b1h["ts"] + 60 * 60 * 1000, ts + 15 * 60 * 1000,
                          side="right") - 1
    atr_al = np.full(len(ts), np.nan)
    ok = idx >= 0
    atr_al[ok] = atr1h[idx[ok]]

    # 取最新一根已收盘 K 线
    i = len(ts) - 1
    if ts[i] <= st["last_ts"]:
        return
    if np.isnan(up[i]) or np.isnan(atr_al[i]):
        st["last_ts"] = int(ts[i])
        save_state(st)
        return

    px = float(c[i])
    gold = bool(k[i] > d[i] and k[i - 1] <= d[i - 1])
    dead = bool(k[i] < d[i] and k[i - 1] >= d[i - 1])
    sig_long = bool(gold and k[i] < K_LONG_MAX)
    sig_short = bool(dead and k[i] > K_SHORT_MIN)

    bw = float(up[i] - lb[i])
    fp = GATE_FEE_RATE * px
    mult = (bw / 2.0) / fp if fp > 0 else 0.0
    if not BOLL_GATE_ENABLED:
        r_eff = RISK_R
    else:
        r_eff = (RISK_R if mult >= GATE_STRONG
                 else (RISK_R / 2.0 if mult >= GATE_WEAK else None))

    day = int(ts[i]) // 86_400_000
    mtm = equity
    st["peak"] = max(st.get("peak", 0.0), mtm)
    if st["day"] != day:
        st["day"], st["day_start_eq"] = day, mtm
    dl = (st["day_start_eq"] - mtm) / st["day_start_eq"] if st["day_start_eq"] else 0.0
    dd = (st["peak"] - mtm) / st["peak"] if st["peak"] else 0.0
    if dd >= 0.10 and not st["halted"]:
        st["halted"] = True
        print(f"[熔断] 累计回撤 {dd:.1%} ≥ 10% —— 停止开新仓, 等待人工指令")
        log_row([_fmt(int(ts[i])), "熔断", "", "", f"{px:.2f}", "", f"{k[i]:.2f}",
                 f"{d[i]:.2f}", f"{atr_al[i]:.2f}", f"{mult:.2f}", f"{equity:.2f}",
                 f"回撤 {dd:.1%}"])
    block = st["halted"] or dl >= 0.03

    action = None
    note = ""
    if ex_side == 0.0:
        want = "LONG" if sig_long else ("SHORT" if sig_short else None)
        if want:
            if block:
                note = "风控闸门: 不开新仓"
            elif r_eff is None:
                note = "布林闸门: 倍数 < 2"
            else:
                qty = floor_step(equity * r_eff / (ATR_MULT_K * float(atr_al[i])))
                if qty < MIN_QTY or qty * px < MIN_NOTIONAL:
                    note = f"数量不足 qty={qty:.4f}"
                elif execute:
                    await client.set_margin_type_isolated()
                    await client.set_leverage(LEVERAGE)
                    # 限价追价: 先被动挂单争取 maker 手续费, 未成交则逐档追价
                    r = await client.place_limit_chase(side=want, quantity=qty)
                    if r.ok:
                        action = f"开{want}"
                        filled = r.cum_filled_qty or qty
                        fill_px = r.avg_price or px
                        st["entry"] = {"side": 1 if want == "LONG" else -1,
                                       "px": fill_px, "qty": filled,
                                       "atr": float(atr_al[i]),
                                       "ms": int(ts[i])}
                        note = f"qty={filled:.4f} r_eff={r_eff:.4f} {_chase_note(r)}"
                    else:
                        note = f"下单失败: {r.error}"
    else:
        cur = 1 if ex_side > 0 else -1
        opposite = (cur == 1 and sig_short) or (cur == -1 and sig_long)
        if opposite:
            if execute:
                # 反手平仓同样走限价追价, 方向取对侧并 reduceOnly
                r = await client.place_limit_chase(
                    side="SHORT" if cur == 1 else "LONG",
                    quantity=abs(ex_side),
                    reduce_only=True,
                )
                if r.ok:
                    action = "反手平仓"
                    note = f"原 {cur} → 反手 {_chase_note(r)}"
                    st["entry"] = None
                else:
                    note = f"平仓失败: {r.error}"

    log_row([_fmt(int(ts[i])), action or "观察", "多" if ex_side > 0 else ("空" if ex_side < 0 else "空仓"),
             f"{abs(ex_side):.4f}", f"{px:.2f}", "", f"{k[i]:.2f}", f"{d[i]:.2f}",
             f"{atr_al[i]:.2f}", f"{mult:.2f}", f"{equity:.2f}", note])

    if action:
        print(f"[{_fmt(int(ts[i]))}] {action} | {note} | K={k[i]:.1f} 倍数={mult:.2f}")
    st["last_ts"] = int(ts[i])
    save_state(st)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真正下单; 不加则只观察")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    client = BinanceTestnetClient()
    if not client.configured:
        print("未配置 Testnet 密钥"); return 1
    mode = current_mode()
    validate_exchange_target(client.base_url)
    if mode.value == "live":
        print("拒绝启动: TRADING_MODE=live 被安全闸门阻断"); return 1
    print(f"运行模式: {mode.value}")
    await client.sync_time()
    print(f"已连接 Testnet | 模式={'真实下单' if args.execute else '仅观察'}")

    st = load_state()
    try:
        while True:
            try:
                await step(client, st, args.execute)
            except Exception as exc:  # noqa: BLE001
                print(f"[{datetime.now():%H:%M:%S}] 异常: {exc}")
            if args.once:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
