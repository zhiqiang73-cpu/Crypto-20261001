"""KDJ 交叉策略的币安合约测试网运行器。
当前规则:
    15m 收盘判定 → 下一根开盘下单
    做多 = 金叉; 做空 = 死叉; 不使用 K 数值阈值
    出场 = 对侧交叉, 先平仓，经交易所确认归零后再开反向仓
    仓位 = 权益 × r_eff ÷ (2 × ATR_1H), r=RISK_R, 向下取整 0.001
    开仓/平仓一律限价: post-only 贴盘口挂单争取 maker, 窗口耗尽才穿盘口兜底
    布林带仅记录 / 日亏 3% / 回撤 10% / 10x 逐仓
    注意：灾难止损仍未在本运行器中持续执行，不能当作已受保护。

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
from typing import Optional

import numpy as np

from shadow.engine import (ATR_MULT_K, BOLL_GATE_ENABLED, DISASTER_ATR, GATE_FEE_RATE,
                           GATE_STRONG, GATE_WEAK, LEVERAGE,
                           MIN_NOTIONAL, MIN_QTY, RISK_R, floor_step)
from shadow.indicators import atr_wilder, boll, kdj
from shadow.signals import crossing
from shadow.live import ENDPOINTS, MARKET, fetch
from trading.binance_client import BinanceTestnetClient
from trading.runtime_mode import current_mode, validate_exchange_target
from config.market_endpoints import (MARKET_MAINNET, MarketMismatchError,
                                     assert_market_consistency,
                                     resolve_for_account)

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "runtime", "shadow")
TRADE_LOG = os.path.join(OUT, "deployed_trades.csv")
STATE = os.path.join(OUT, "deployed_state.json")
# 策略「此刻的读数」快照 —— 供面板展示, 便于用户拿它和图表逐项核对。
READING = os.path.join(OUT, "latest_reading.json")
# 策略自己下过的委托号台账 —— 用于把「策略单」与「功能测试单」可靠分开。
ORDER_LEDGER = os.path.join(OUT, "deployed_orders.jsonl")
# 运行器心跳 —— 页面据此区分「最新读数」和「运行器确实仍在运行」。
HEARTBEAT = os.path.join(OUT, "runner_heartbeat.json")

COLS = ["时间", "动作", "方向", "数量", "价格", "净盈亏", "K", "D", "ATR_1H",
        "倍数", "权益", "说明"]

# 本运行器的 clientOrderId 前缀。没有它, 账户里策略单和测试单无法区分 ——
# 2026-10-02 用户看到 10:22 的 5 笔成交以为策略发了 5 次信号, 就是缺这个标签。
ORDER_TAG = "kdj"

# 补记窗口: 运行器停机后最多回看多少根 15m K 线 (96 根 = 24 小时)。
# 2026-10-02 用户在图表上看到 09:45 金叉, 而日志里那一根只有「观察」——
# 信号判定本身没错 (当时规则仍要求 K<30, 该根 K=50.73 不满足),
# 但「运行器停了多久、跳过了哪几根」当时完全无从查起。
MISSED_LOOKBACK_BARS = 96


def plan_pending(ts, last_ts: int, *, lookback: int = MISSED_LOOKBACK_BARS):
    """规划本轮要处理的已收盘 K 线。

    返回 (需要补记的下标, 最新下标, 因超出回看窗口而丢弃的根数)。
    最新下标为 None 表示没有新收盘的 K 线。

    存在的意义: 旧实现只处理「最新一根」, 运行器停机期间收盘的 K 线
    会被静默跳过 —— 既不下单, 也不留痕, 事后无法判断是否漏过信号。
    现在这些 K 线会被逐根补记为「错过」并计入统计。
    """
    if len(ts) == 0:
        return [], None, 0
    idx = [j for j in range(len(ts)) if int(ts[j]) > int(last_ts)]
    if not idx:
        return [], None, 0
    missed, latest = idx[:-1], idx[-1]
    dropped = 0
    if lookback > 0 and len(missed) > lookback:
        dropped = len(missed) - lookback
        missed = missed[-lookback:]
    return missed, latest, dropped


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _chase_note(r) -> str:
    """把被动挂单过程转成一行可读备注 (maker/taker + 挂单次数 + 成交价).

    用于事后核对「是否真的吃到了 maker 手续费」—— 不靠承诺, 靠日志。
    maker 判定不是推断: 被动阶段用 post-only (期货 TIF=GTX, 会立即成交则
    交易所直接拒单 -5022), 交易所层面保证成交即 maker; 兜底阶段穿盘口,
    必为 taker。
    """
    meta = (r.raw or {}).get("chase") if isinstance(r.raw, dict) else None
    if not meta:
        return ""
    tag = "maker" if meta.get("likely_maker") else "taker"
    return (f"{tag} 被动{meta.get('passive_attempts', 0)}次 "
            f"成交价={r.avg_price:.2f} 总单数={meta.get('steps_used', 0)}")


def log_row(row: list) -> None:
    os.makedirs(OUT, exist_ok=True)
    new = not os.path.exists(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLS)
        w.writerow(row)


def record_order(r, *, action: str) -> None:
    """把策略自己下的委托号追加到台账。

    只记录「策略确实下出去的单」。账户历史里还有功能测试单和人工单,
    只有策略自己留下委托号, 事后才能可靠回答「策略赚了多少」。
    记录失败不影响交易, 但会让该笔单退化为「未判定」来源 —— 绝不猜测。
    """
    oid = str(getattr(r, "order_id", "") or "").strip()
    if not oid:
        return
    rec = {
        "order_id": oid,
        "client_order_id": str(getattr(r, "client_order_id", "") or ""),
        "action": action,
        "ok": bool(getattr(r, "ok", False)),
        "filled": float(getattr(r, "cum_filled_qty", 0.0) or 0.0),
        "avg_price": float(getattr(r, "avg_price", 0.0) or 0.0),
        "ts_ms": int(time.time() * 1000),
        "market": MARKET,
    }
    os.makedirs(OUT, exist_ok=True)
    try:
        with open(ORDER_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


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


def save_reading(rec: dict) -> None:
    """落盘最新一根已收盘 K 线的完整读数。

    存在的意义: 用户核对信号时, 必须能确认「bot 读的是哪个市场、哪一根、
    哪几个数」。2026-10-02 的市场错位之所以难查, 就是因为没有这个快照。
    """
    os.makedirs(OUT, exist_ok=True)
    try:
        with open(READING + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(READING + ".tmp", READING)
    except Exception:  # noqa: BLE001
        pass


def save_heartbeat(*, status: str, execute: bool, detail: str = "") -> None:
    """原子写入运行器心跳，供面板判断是否因进程退出而暂停。"""
    os.makedirs(OUT, exist_ok=True)
    rec = {
        "status": status,
        "mode": "testnet_orders" if execute else "observation_only",
        "updated_ms": int(time.time() * 1000),
        "detail": detail,
        "market": MARKET,
        "symbol": "BTCUSDT",
        "interval_sec": 15,
    }
    try:
        with open(HEARTBEAT + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(HEARTBEAT + ".tmp", HEARTBEAT)
    except Exception:  # noqa: BLE001
        pass


def save_signal_reading(st: dict, *, i: int, ts, o, h, l, c, k, d, atr_al,
                        up, lb, now: int, execute: bool, pos_side: str) -> None:
    """写入当前最新已收盘 K 线的读数，不依赖它是不是「新 K 线」。

    即使运行器重启时这根 K 线已经被处理过，也要刷新快照；否则进程崩溃在
    JSON 写入前会造成「日志是新的、页面读数却是旧的」的假象。
    """
    px = float(c[i])
    sig_long = sig_short = False
    if i > 0:
        sig_long, sig_short = crossing(k[i - 1], d[i - 1], k[i], d[i])
    band_width = float(up[i] - lb[i])
    mult = (band_width / 2.0) / (GATE_FEE_RATE * px) if px > 0 else 0.0

    def finite(v):
        value = float(v)
        return value if np.isfinite(value) else None

    save_reading({
        "bar_utc": _fmt(int(ts[i])),
        "bar_ms": int(ts[i]),
        "open": finite(o[i]), "high": finite(h[i]),
        "low": finite(l[i]), "close": finite(px),
        "K": finite(k[i]), "D": finite(d[i]),
        "ATR_1H": finite(atr_al[i]),
        "mult": finite(mult),
        # `crossing()` 接收 NumPy 标量时会返回 numpy.bool_；json 不认识它。
        # 若不显式转换, save_reading 会静默失败、面板永远显示旧 K 线。
        "signal_long": bool(sig_long), "signal_short": bool(sig_short),
        "signal_rule": "金叉做多 / 死叉做空；不使用 K 极值过滤",
        "signal_needs_k_extreme": False,
        "missed_bars": int(st.get("missed_bars", 0)),
        "missed_signals": int(st.get("missed_signals", 0)),
        "run_mode_at_snapshot": "testnet_orders" if execute else "observation_only",
        "position": "多" if pos_side == "LONG" else (
            "空" if pos_side == "SHORT" else "空仓"),
        "market": MARKET,
        "symbol": "BTCUSDT", "interval": "15m",
        "kline_url": ENDPOINTS.rest + "/fapi/v1/klines",
        "ws": ENDPOINTS.ws,
        "account_base_url": ENDPOINTS.account_base_url,
        "updated_ms": now,
    })


async def prepare_testnet_execution(client: BinanceTestnetClient) -> dict:
    """让人工启动的 Testnet 执行器进入确定、可审计的账户状态。

    检查顺序刻意保守：

    1. 有外部挂单时拒绝启动，绝不擅自取消；
    2. 需要改持仓模式 / 逐仓 / 杠杆但账户有仓时拒绝启动，绝不混改；
    3. 空仓且没有挂单时才把账户设置成策略规格的「单向、10x、逐仓」；
    4. 每个写入操作之后重新读取并验证，不是只相信接口没有报错。

    本函数只有 `--execute` 才调用。观察模式严格只读。
    """
    orders = await client.get_open_orders()
    pos = await client.get_position()
    has_position = abs(float(getattr(pos, "quantity", 0.0) or 0.0)) > 1e-12
    if orders:
        raise RuntimeError(
            f"检测到 {len(orders)} 笔 BTCUSDT 未完成委托；拒绝启动策略以免混单。"
            "请在交易所自行确认/处理后再启动。"
        )

    hedge = await client.get_position_mode()
    settings = await client.get_position_settings()
    needs_change = (hedge or not settings.get("isolated")
                    or int(settings.get("leverage") or 0) != LEVERAGE)
    if has_position and needs_change:
        raise RuntimeError(
            "现有仓位与策略账户设置不一致；拒绝在有仓时切换单向/逐仓/杠杆。"
            f"当前: hedge={hedge}, {settings.get('margin_type')}, "
            f"{settings.get('leverage')}x；目标: 单向, ISOLATED, {LEVERAGE}x。"
        )

    changes = []
    if hedge:
        await client.set_one_way_mode()
        if await client.get_position_mode():
            raise RuntimeError("无法确认账户已切换为单向持仓，拒绝启动策略。")
        changes.append("单向持仓")

    if not settings.get("isolated"):
        await client.set_margin_type_isolated()
        settings = await client.get_position_settings()
        if not settings.get("isolated"):
            raise RuntimeError("无法确认 BTCUSDT 已设为逐仓，拒绝启动策略。")
        changes.append("ISOLATED")

    if int(settings.get("leverage") or 0) != LEVERAGE:
        await client.set_leverage(LEVERAGE)
        settings = await client.get_position_settings()
        if int(settings.get("leverage") or 0) != LEVERAGE:
            raise RuntimeError(
                f"无法确认 BTCUSDT 已设为 {LEVERAGE}x，拒绝启动策略。"
            )
        changes.append(f"{LEVERAGE}x")

    return {
        "position_mode": "one_way",
        "margin_type": settings.get("margin_type"),
        "leverage": int(settings.get("leverage") or 0),
        "changes": changes,
    }


async def disaster_limit_stop(client: BinanceTestnetClient, st: dict, *,
                              ex_side: float, entry_px: float,
                              atr_1h: float, execute: bool) -> Optional[dict]:
    """在浮亏达到 3×ATR 时立即平仓。

    原始规格要求灾难止损强制退出；后续用户要求所有委托使用限价单。因此这里
    使用 `force_cross=True` 的穿盘口 **LIMIT** 单：不等待普通订单的 180 秒
    maker 窗口，但依旧带 `PASSIVE_CROSS_TICKS` 的价格上限，绝不发送 MARKET。

    只在 execute 模式调用；观察模式严格不产生任何委托。
    返回 None 表示未触发，返回字典表示已尝试下单（无论最终是否全部成交）。
    """
    if not execute or ex_side == 0.0 or not np.isfinite(float(atr_1h)):
        return None
    if entry_px <= 0 or atr_1h <= 0:
        return None
    mark = float(await client.mark_price())
    if mark <= 0:
        return None
    loss_points = (entry_px - mark) if ex_side > 0 else (mark - entry_px)
    stop_points = DISASTER_ATR * float(atr_1h)
    if loss_points < stop_points:
        return None

    close_side = "SHORT" if ex_side > 0 else "LONG"
    result = await client.place_limit_chase(
        side=close_side, quantity=abs(ex_side), reduce_only=True,
        tag=ORDER_TAG, force_cross=True,
    )
    record_order(result, action="灾难止损平仓")
    after = await client.get_position()
    remaining = abs(float(getattr(after, "quantity", 0.0) or 0.0))
    flat = remaining < MIN_QTY
    if flat:
        st["entry"] = None
    detail = (
        f"浮亏 {loss_points:.2f} ≥ {DISASTER_ATR:.0f}×ATR {stop_points:.2f}; "
        f"LIMIT 强平 {_chase_note(result)}; 剩余 {remaining:.4f} BTC"
    )
    return {
        "action": "灾难止损平仓" if flat else "灾难止损部分平仓",
        "note": detail,
        "mark": mark,
        "flat": flat,
        "remaining": remaining,
        "result": result,
    }


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

    # 灾难止损每 15 秒检查一次，不等下一根 K 线，也不等反向信号。
    # 触发后本轮不再反手，避免止损和新开仓在同一个价格区间互相打架。
    latest_i = len(ts) - 1
    emergency = await disaster_limit_stop(
        client, st, ex_side=ex_side, entry_px=entry_px,
        atr_1h=float(atr_al[latest_i]), execute=execute,
    )
    if emergency:
        log_row([_fmt(int(ts[latest_i])), emergency["action"],
                 "多" if ex_side > 0 else "空", f"{abs(ex_side):.4f}",
                 f"{emergency['mark']:.2f}", "", f"{k[latest_i]:.2f}",
                 f"{d[latest_i]:.2f}", f"{atr_al[latest_i]:.2f}", "",
                 f"{equity:.2f}", emergency["note"]])
        save_signal_reading(
            st, i=latest_i, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d,
            atr_al=atr_al, up=up, lb=lb, now=now, execute=execute,
            pos_side="FLAT" if emergency["flat"] else pos_side,
        )
        st["last_ts"] = int(ts[latest_i])
        save_state(st)
        return

    # 取最新一根已收盘 K 线; 停机期间收盘的先逐根补记, 绝不静默丢弃。
    missed, i, dropped = plan_pending(ts, st["last_ts"])
    if i is None:
        # 没有新 K 线也刷新读数。读数是面板核对入口，不应因同一根 K 线
        # 已被处理过而停在旧版本。
        save_signal_reading(
            st, i=len(ts) - 1, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d,
            atr_al=atr_al, up=up, lb=lb, now=now, execute=execute,
            pos_side=pos_side,
        )
        return
    if missed:
        sigs = 0
        for j in missed:
            gold = dead = False
            if j > 0:
                gold, dead = crossing(k[j - 1], d[j - 1], k[j], d[j])
            if gold or dead:
                sigs += 1
            which = "金叉做多" if gold else ("死叉做空" if dead else "无交叉")
            log_row([_fmt(int(ts[j])), "错过",
                     "多" if ex_side > 0 else ("空" if ex_side < 0 else "空仓"),
                     f"{abs(ex_side):.4f}", f"{float(c[j]):.2f}", "",
                     f"{k[j]:.2f}", f"{d[j]:.2f}", f"{atr_al[j]:.2f}",
                     "", "", f"运行器未运行期间收盘; 该根 {which}, 未下单"])
        st["missed_bars"] = int(st.get("missed_bars", 0)) + len(missed)
        st["missed_signals"] = int(st.get("missed_signals", 0)) + sigs
        print(f"[补记] 停机期间收盘 {len(missed)} 根, 其中 {sigs} 根有交叉信号"
              + (f"; 另有 {dropped} 根超出回看窗口未补记" if dropped else ""))
    if np.isnan(up[i]) or np.isnan(atr_al[i]):
        st["last_ts"] = int(ts[i])
        save_state(st)
        return

    px = float(c[i])
    sig_long, sig_short = crossing(k[i - 1], d[i - 1], k[i], d[i])

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
                    # 启动阶段 `prepare_testnet_execution()` 已经读回验证单向、
                    # 逐仓和 10x。信号发生时不重复改账户设置，避免把下单路径
                    # 与账户配置写操作混在一起。
                    # 限价追价: 先被动挂单争取 maker 手续费, 未成交则逐档追价
                    r = await client.place_limit_chase(
                        side=want, quantity=qty, tag=ORDER_TAG)
                    record_order(r, action=f"开{want}")
                    if r.ok and r.cum_filled_qty > 0:
                        action = f"开{want}"
                        filled = r.cum_filled_qty
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
                    tag=ORDER_TAG,
                )
                record_order(r, action="反手平仓")
                # 只能在交易所确认旧仓**完全归零**后才尝试新方向。
                # 部分成交、撤单竞态、外部手动订单均不得被当成已平仓。
                after = await client.get_position()
                remaining = abs(float(getattr(after, "quantity", 0.0) or 0.0))
                if (not r.ok or r.cum_filled_qty <= 0 or remaining >= MIN_QTY):
                    note = (f"反向平仓未确认: 剩余 {remaining:.4f} BTC; "
                            f"本单已成交 {r.cum_filled_qty:.4f}; {r.error}")
                    action = "部分平仓" if r.cum_filled_qty > 0 else None
                else:
                    st["entry"] = None
                    action = "平仓"
                    note = f"原 {cur} 已归零 {_chase_note(r)}"
                    # 先完成平仓再判断风控，绝不因不允许开新仓而阻止平旧仓。
                    if block or r_eff is None:
                        note += "; 风控阻止反手开新仓"
                    else:
                        fresh_bal = await client.get_balance()
                        fresh_equity = float(fresh_bal.total_wallet_balance)
                        qty = floor_step(fresh_equity * r_eff /
                                         (ATR_MULT_K * float(atr_al[i])))
                        if qty < MIN_QTY or qty * px < MIN_NOTIONAL:
                            note += f"; 反手数量不足 {qty:.4f}"
                        else:
                            want = "SHORT" if cur == 1 else "LONG"
                            opened = await client.place_limit_chase(
                                side=want, quantity=qty, tag=ORDER_TAG)
                            record_order(opened, action=f"反手开{want}")
                            actual = await client.get_position()
                            if (getattr(actual, "side", "FLAT") or "FLAT").upper() == want \
                                    and float(getattr(actual, "quantity", 0) or 0) > 0:
                                filled = float(actual.quantity)
                                st["entry"] = {"side": -cur,
                                               "px": float(actual.entry_price),
                                               "qty": filled, "atr": float(atr_al[i]),
                                               "ms": int(ts[i])}
                                action = f"平仓并开{want}"
                                note += f"; 反手 {filled:.4f} {_chase_note(opened)}"
                            else:
                                note += f"; 反手未建立: {opened.error}"

    log_row([_fmt(int(ts[i])), action or "观察", "多" if ex_side > 0 else ("空" if ex_side < 0 else "空仓"),
             f"{abs(ex_side):.4f}", f"{px:.2f}", "", f"{k[i]:.2f}", f"{d[i]:.2f}",
             f"{atr_al[i]:.2f}", f"{mult:.2f}", f"{equity:.2f}", note])

    if action:
        print(f"[{_fmt(int(ts[i]))}] {action} | {note} | K={k[i]:.1f} 倍数={mult:.2f}")

    # 读数快照: 面板据此展示「bot 此刻读的是哪个市场、哪一根、哪几个数」。
    save_signal_reading(
        st, i=i, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d, atr_al=atr_al,
        up=up, lb=lb, now=now, execute=execute, pos_side=pos_side,
    )
    st["last_ts"] = int(ts[i])
    save_state(st)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真正下单; 不加则只观察")
    # 轮询间隔从 60s 收紧到 15s: 被动挂单要靠"早"才吃得到 maker,
    # 每根 15m K 线只判一次信号, 判到就应立刻挂到盘口。
    ap.add_argument("--interval", type=int, default=15)
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

    # ---- 市场一致性闸门 ----------------------------------------------------
    # 2026-10-02 事故的硬性防复发措施: 行情腿与下单腿必须同市场。
    # 曾经 K 线写死主网、下单走测试网, 信号错位 2 根 K 线(30 分钟),
    # 同一笔空单毛利从 +103.5 点掉到 +37.0 点。不一致就拒绝启动, 不做任何交易。
    ep = resolve_for_account()
    print(f"行情腿: {ep.label}   K线基准={ep.rest}   WS={ep.ws}")
    print(f"下单腿: {client.base_url}")
    print(f"地址来源: {ep.source}")
    try:
        assert_market_consistency(client.base_url, ep.rest)
        assert_market_consistency(client.base_url, ep.ws)
    except MarketMismatchError as exc:
        print(f"[拒绝启动] {exc}")
        return 1
    if ep.market == MARKET_MAINNET:
        print("[拒绝启动] 行情腿指向主网, 但本框架只允许测试网验证")
        return 1

    await client.sync_time()
    print(f"已连接 {ep.label} | 模式={'真实下单' if args.execute else '仅观察'}")

    if args.execute:
        try:
            prepared = await prepare_testnet_execution(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[拒绝启动] 执行账户预检失败: {exc}")
            save_heartbeat(status="blocked", execute=True,
                           detail=f"执行账户预检失败: {type(exc).__name__}: {exc}")
            await client.close()
            return 1
        changed = (f"（已设置: {', '.join(prepared['changes'])}）"
                   if prepared["changes"] else "（已符合策略规格）")
        print("执行账户预检通过: "
              f"{prepared['position_mode']} / {prepared['margin_type']} / "
              f"{prepared['leverage']}x {changed}")

    st = load_state()
    st["market"] = ep.market
    st["market_rest"] = ep.rest
    save_state(st)
    save_heartbeat(status="starting", execute=args.execute,
                   detail="市场一致性与启动预检通过")
    try:
        while True:
            try:
                await step(client, st, args.execute)
                save_heartbeat(status="running", execute=args.execute)
            except Exception as exc:  # noqa: BLE001
                print(f"[{datetime.now():%H:%M:%S}] 异常: {exc}")
                save_heartbeat(status="error", execute=args.execute,
                               detail=f"{type(exc).__name__}: {exc}")
            if args.once:
                save_heartbeat(status="stopped", execute=args.execute,
                               detail="--once 已完成")
                return 0
            await asyncio.sleep(args.interval)
    finally:
        save_heartbeat(status="stopped", execute=args.execute,
                       detail="运行器进程退出")
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
