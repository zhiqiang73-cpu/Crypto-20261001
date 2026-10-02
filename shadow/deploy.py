"""KDJ 交叉策略的币安合约测试网运行器。

BTCUSDT 与 ETHUSDT 各跑两条策略，按标的各记虚拟仓、只下该标的净额：
    15m: 金叉做多 / 死叉做空，无 K 阈值
    5m:  金叉且 K<30 做多 / 死叉且 K>70 做空
    仓位 = 权益 × r ÷ (2 × ATR_1H), r=RISK_R, 向下取整 0.001
    开仓/平仓一律限价: post-only 贴盘口挂单争取 maker, 窗口耗尽才穿盘口兜底
    布林带仅记录 / 日亏 3% / 回撤 10% / 10x 逐仓

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

from shadow.engine import (ATR_MULT_K, DISASTER_ATR, GATE_FEE_RATE,
                           LEVERAGE, MIN_QTY, RISK_R, floor_step)
from shadow.indicators import atr_wilder, boll, kdj
from shadow.signals import entry_signal
from shadow.live import ENDPOINTS, MARKET, fetch
from shadow.strategy_books import (SPEC_15M, SPEC_5M, SPECS, TRADE_SYMBOLS,
                                   apply_virtual_signal, clear_symbol_books,
                                   desired_net, migrate_state,
                                   reduce_only_for_delta, signal_reason,
                                   specs_for_symbol, symbol_short)
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
READING_5M = os.path.join(OUT, "latest_reading_5m.json")
READING_ETH15 = os.path.join(OUT, "latest_reading_eth15.json")
READING_ETH5 = os.path.join(OUT, "latest_reading_eth5.json")
READING_BY_SPEC = {
    "kdj15": lambda: READING,
    "kdj5": lambda: READING_5M,
    "eth15": lambda: READING_ETH15,
    "eth5": lambda: READING_ETH5,
}
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
        "symbol": str(getattr(r, "symbol", "") or ""),
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
            st = json.load(fh)
    else:
        st = {"last_ts": 0, "peak": 0.0, "day": None, "day_start_eq": 0.0,
              "halted": False, "entry": None}
    return migrate_state(st)


def save_state(st: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(STATE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2)
    os.replace(STATE + ".tmp", STATE)


def save_reading(rec: dict, path: Optional[str] = None) -> None:
    """落盘最新一根已收盘 K 线的完整读数。

    存在的意义: 用户核对信号时, 必须能确认「bot 读的是哪个市场、哪一根、
    哪几个数」。2026-10-02 的市场错位之所以难查, 就是因为没有这个快照。
    """
    target = path or READING
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    try:
        with open(target + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(target + ".tmp", target)
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
        "symbols": list(TRADE_SYMBOLS),
        "interval_sec": 15,
        "strategies": [spec.id for spec in SPECS],
    }
    try:
        with open(HEARTBEAT + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(HEARTBEAT + ".tmp", HEARTBEAT)
    except Exception:  # noqa: BLE001
        pass


def save_signal_reading(st: dict, *, i: int, ts, o, h, l, c, k, d, atr_al,
                        up, lb, now: int, execute: bool, pos_side: str,
                        interval: str = "15m",
                        signal_rule: str = "金叉做多 / 死叉做空；不使用 K 极值过滤",
                        k_long_max=None, k_short_min=None,
                        path: Optional[str] = None,
                        symbol: str = "BTCUSDT") -> None:
    """写入当前最新已收盘 K 线的读数，不依赖它是不是「新 K 线」。

    即使运行器重启时这根 K 线已经被处理过，也要刷新快照；否则进程崩溃在
    JSON 写入前会造成「日志是新的、页面读数却是旧的」的假象。
    """
    px = float(c[i])
    sig_long = sig_short = gold = dead = False
    if i > 0:
        sig_long, sig_short, gold, dead = entry_signal(
            k[i - 1], d[i - 1], k[i], d[i],
            k_long_max=k_long_max, k_short_min=k_short_min,
        )
    band_width = float(up[i] - lb[i])
    mult = (band_width / 2.0) / (GATE_FEE_RATE * px) if px > 0 else 0.0

    def finite(v):
        value = float(v)
        return value if np.isfinite(value) else None

    start = max(0, i + 1 - 36)
    series_k = [finite(k[j]) for j in range(start, i + 1)]
    series_d = [finite(d[j]) for j in range(start, i + 1)]

    rec = {
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
        "gold": bool(gold), "dead": bool(dead),
        "signal_rule": signal_rule,
        "signal_needs_k_extreme": k_long_max is not None or k_short_min is not None,
        "k_long_max": k_long_max,
        "k_short_min": k_short_min,
        "missed_bars": int(st.get("missed_bars", 0)),
        "missed_signals": int(st.get("missed_signals", 0)),
        "run_mode_at_snapshot": "testnet_orders" if execute else "observation_only",
        "position": "多" if pos_side == "LONG" else (
            "空" if pos_side == "SHORT" else "空仓"),
        "market": MARKET,
        "symbol": symbol, "interval": interval,
        "kline_url": ENDPOINTS.rest + "/fapi/v1/klines",
        "ws": ENDPOINTS.ws,
        "account_base_url": ENDPOINTS.account_base_url,
        "updated_ms": now,
        "series": {"k": series_k, "d": series_d},
    }
    save_reading(rec, path)


async def prepare_testnet_execution(client: BinanceTestnetClient) -> dict:
    """让人工启动的 Testnet 执行器进入确定、可审计的账户状态。

    检查顺序刻意保守：

    1. 任一标的有外部挂单时拒绝启动，绝不擅自取消；
    2. 需要改持仓模式 / 逐仓 / 杠杆但该标的有仓时拒绝启动，绝不混改；
    3. 空仓且没有挂单时才把该标的设成策略规格的「单向、10x、逐仓」；
    4. 每个写入操作之后重新读取并验证，不是只相信接口没有报错。

    本函数只有 `--execute` 才调用。观察模式严格只读。
    """
    leftover = []
    for symbol in TRADE_SYMBOLS:
        leftover.extend(await client.get_open_orders(symbol))
    if leftover:
        raise RuntimeError(
            f"检测到 {len(leftover)} 笔未完成委托；拒绝启动策略以免混单。"
            "请在交易所自行确认/处理后再启动。"
        )

    hedge = await client.get_position_mode()
    settings_by_symbol = {}
    occupied = []
    for symbol in TRADE_SYMBOLS:
        pos = await client.get_position(symbol)
        settings = await client.get_position_settings(symbol)
        settings_by_symbol[symbol] = settings
        has_position = abs(float(getattr(pos, "quantity", 0.0) or 0.0)) > 1e-12
        needs_change = (hedge or not settings.get("isolated")
                        or int(settings.get("leverage") or 0) != LEVERAGE)
        if has_position and needs_change:
            occupied.append(
                f"{symbol}: hedge={hedge}, {settings.get('margin_type')}, "
                f"{settings.get('leverage')}x"
            )
    if occupied:
        raise RuntimeError(
            "现有仓位与策略账户设置不一致；拒绝在有仓时切换单向/逐仓/杠杆。"
            f"当前: {'; '.join(occupied)}；目标: 单向, ISOLATED, {LEVERAGE}x。"
        )

    changes = []
    if hedge:
        await client.set_one_way_mode()
        if await client.get_position_mode():
            raise RuntimeError("无法确认账户已切换为单向持仓，拒绝启动策略。")
        changes.append("单向持仓")

    last_settings = settings_by_symbol[TRADE_SYMBOLS[0]]
    for symbol in TRADE_SYMBOLS:
        settings = await client.get_position_settings(symbol)
        if not settings.get("isolated"):
            await client.set_margin_type_isolated(symbol)
            settings = await client.get_position_settings(symbol)
            if not settings.get("isolated"):
                raise RuntimeError(f"无法确认 {symbol} 已设为逐仓，拒绝启动策略。")
            changes.append(f"{symbol} ISOLATED")
        if int(settings.get("leverage") or 0) != LEVERAGE:
            await client.set_leverage(LEVERAGE, symbol)
            settings = await client.get_position_settings(symbol)
            if int(settings.get("leverage") or 0) != LEVERAGE:
                raise RuntimeError(
                    f"无法确认 {symbol} 已设为 {LEVERAGE}x，拒绝启动策略。"
                )
            changes.append(f"{symbol} {LEVERAGE}x")
        settings_by_symbol[symbol] = settings
        last_settings = settings

    return {
        "position_mode": "one_way",
        "margin_type": last_settings.get("margin_type"),
        "leverage": int(last_settings.get("leverage") or 0),
        "changes": changes,
        "symbols": list(TRADE_SYMBOLS),
    }


def _closed_bars(bars: dict, interval_ms: int, now: int) -> dict:
    mask = (bars["ts"] + interval_ms) <= now
    return {key: value[mask] for key, value in bars.items()}


def _align_atr(ts, interval_ms: int, bars1h: dict, atr1h):
    idx = np.searchsorted(bars1h["ts"] + 60 * 60 * 1000,
                          ts + interval_ms, side="right") - 1
    out = np.full(len(ts), np.nan)
    ok = idx >= 0
    out[ok] = atr1h[idx[ok]]
    return out


def _virtual_side_label(book: dict) -> str:
    qty = float(((book.get("entry") or {}).get("qty")) or 0.0)
    side = (book.get("entry") or {}).get("side")
    if qty <= 0 or side in (0, None, "FLAT"):
        return "FLAT"
    return "LONG" if float(side) > 0 else "SHORT"


async def sync_net(client: BinanceTestnetClient, st: dict, execute: bool, *,
                   symbol: str, tag: str, force_cross: bool = False):
    """把同一标的两条策略的虚拟仓合成净仓，只下差额。"""
    pos = await client.get_position(symbol)
    pos_side = (getattr(pos, "side", "FLAT") or "FLAT").upper()
    ex = float(getattr(pos, "quantity", 0.0) or 0.0) * (
        1.0 if pos_side == "LONG" else (-1.0 if pos_side == "SHORT" else 0.0))
    desired = desired_net(st, symbol)
    delta = desired - ex
    if abs(delta) < MIN_QTY:
        return None
    if not execute:
        print(f"[{symbol} 净仓观察] 应有 {desired:.4f} 实际 {ex:.4f} 差额 {delta:.4f}")
        return None
    side = "LONG" if delta > 0 else "SHORT"
    reduce_only = reduce_only_for_delta(ex, desired, MIN_QTY)
    result = await client.place_limit_chase(
        side=side, quantity=abs(delta), reduce_only=reduce_only,
        tag=tag, force_cross=force_cross, symbol=symbol,
    )
    record_order(result, action=f"{symbol_short(symbol)}净仓{side}")
    after = await client.get_position(symbol)
    after_side = (getattr(after, "side", "FLAT") or "FLAT").upper()
    after_qty = float(getattr(after, "quantity", 0.0) or 0.0) * (
        1.0 if after_side == "LONG" else (-1.0 if after_side == "SHORT" else 0.0))
    print(f"[{symbol} 净仓] 目标 {desired:.4f} 原 {ex:.4f} → {after_qty:.4f} "
          f"{_chase_note(result) or result.error}")
    return result


def process_strategy(spec, book: dict, bars: dict, atr_al, *,
                     equity: float, block: bool, now: int, execute: bool) -> bool:
    """处理一条策略的已收盘 K 线，只改虚拟账本。返回是否需要同步净仓。"""
    ts = bars["ts"]
    o, h, l, c = (bars[x] for x in ("open", "high", "low", "close"))
    k, d, _j = kdj(h, l, c)
    _mb, up, lb, _ = boll(c, 20, 2.0)
    reading_path = READING_BY_SPEC.get(spec.id, lambda: READING)()
    mark = f"{symbol_short(spec.symbol)} {spec.interval}"

    if spec.cold_start and not book.get("armed"):
        if len(ts):
            book["last_ts"] = int(ts[-1])
        book["armed"] = True
        if len(ts):
            save_signal_reading(
                book, i=len(ts) - 1, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d,
                atr_al=atr_al, up=up, lb=lb, now=now, execute=execute,
                pos_side=_virtual_side_label(book),
                interval=spec.interval, signal_rule=spec.signal_rule,
                k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
                path=reading_path, symbol=spec.symbol,
            )
        print(f"[{mark}] 冷启动，从下一根已收盘 K 线开始交易")
        return False

    missed, i, dropped = plan_pending(ts, int(book.get("last_ts") or 0),
                                      lookback=spec.lookback)
    if i is None:
        if len(ts):
            save_signal_reading(
                book, i=len(ts) - 1, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d,
                atr_al=atr_al, up=up, lb=lb, now=now, execute=execute,
                pos_side=_virtual_side_label(book),
                interval=spec.interval, signal_rule=spec.signal_rule,
                k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
                path=reading_path, symbol=spec.symbol,
            )
        return False

    if missed:
        sigs = 0
        for j in missed:
            gold = dead = False
            if j > 0:
                _lo, _sh, gold, dead = entry_signal(
                    k[j - 1], d[j - 1], k[j], d[j],
                    k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
                )
            if gold or dead:
                sigs += 1
            which = "金叉做多" if gold else ("死叉做空" if dead else "无交叉")
            log_row([_fmt(int(ts[j])), f"{mark}错过",
                     "空仓", "0.0000", f"{float(c[j]):.2f}", "",
                     f"{k[j]:.2f}", f"{d[j]:.2f}", f"{atr_al[j]:.2f}",
                     "", "", f"运行器未运行期间收盘; 该根 {which}, 未下单"])
        book["missed_bars"] = int(book.get("missed_bars", 0)) + len(missed)
        book["missed_signals"] = int(book.get("missed_signals", 0)) + sigs
        print(f"[{mark} 补记] 停机期间收盘 {len(missed)} 根, "
              f"其中 {sigs} 根有交叉"
              + (f"; 另有 {dropped} 根超出回看窗口未补记" if dropped else ""))

    if i <= 0 or np.isnan(atr_al[i]):
        book["last_ts"] = int(ts[i])
        return False

    px = float(c[i])
    sig_long, sig_short, gold, dead = entry_signal(
        k[i - 1], d[i - 1], k[i], d[i],
        k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
    )
    qty = 0.0
    if not np.isnan(atr_al[i]) and atr_al[i] > 0:
        qty = floor_step(equity * RISK_R / (ATR_MULT_K * float(atr_al[i])))
    action, note, changed = apply_virtual_signal(
        book, sig_long=sig_long, sig_short=sig_short, qty=qty,
        px=px, atr=float(atr_al[i]), ms=int(ts[i]), block=block,
        min_qty=MIN_QTY,
        meta={
            "interval": spec.interval,
            "strategy_id": spec.id,
            "reason": signal_reason(
                spec, gold=gold, dead=dead, k=float(k[i]),
            ),
            "k": float(k[i]),
            "d": float(d[i]),
            "signal": "golden_cross" if gold else ("dead_cross" if dead else ""),
            "symbol": spec.symbol,
        },
    )
    if action is None and (gold or dead) and not (sig_long or sig_short):
        note = f"交叉但不满足 K 阈值 (K={k[i]:.2f})"
    log_row([_fmt(int(ts[i])), f"{mark}{action or '观察'}",
             "多" if _virtual_side_label(book) == "LONG" else (
                 "空" if _virtual_side_label(book) == "SHORT" else "空仓"),
             f"{abs(float(((book.get('entry') or {}).get('qty')) or 0)):.4f}",
             f"{px:.2f}", "", f"{k[i]:.2f}", f"{d[i]:.2f}",
             f"{atr_al[i]:.2f}", "", f"{equity:.2f}", note])
    if action:
        print(f"[{mark} {_fmt(int(ts[i]))}] {action} | {note} | "
              f"K={k[i]:.1f}")
    save_signal_reading(
        book, i=i, ts=ts, o=o, h=h, l=l, c=c, k=k, d=d, atr_al=atr_al,
        up=up, lb=lb, now=now, execute=execute,
        pos_side=_virtual_side_label(book),
        interval=spec.interval, signal_rule=spec.signal_rule,
        k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
        path=reading_path, symbol=spec.symbol,
    )
    book["last_ts"] = int(ts[i])
    return changed


async def disaster_limit_stop(client: BinanceTestnetClient, st: dict, *,
                              ex_side: float, entry_px: float,
                              atr_1h: float, execute: bool,
                              symbol: str = "BTCUSDT",
                              tag: str = ORDER_TAG) -> Optional[dict]:
    """在该标的浮亏达到 3×ATR 时立即平仓。

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
    mark = float(await client.mark_price(symbol))
    if mark <= 0:
        return None
    loss_points = (entry_px - mark) if ex_side > 0 else (mark - entry_px)
    stop_points = DISASTER_ATR * float(atr_1h)
    if loss_points < stop_points:
        return None

    close_side = "SHORT" if ex_side > 0 else "LONG"
    result = await client.place_limit_chase(
        side=close_side, quantity=abs(ex_side), reduce_only=True,
        tag=tag, force_cross=True, symbol=symbol,
    )
    record_order(result, action=f"{symbol_short(symbol)}灾难止损平仓")
    after = await client.get_position(symbol)
    remaining = abs(float(getattr(after, "quantity", 0.0) or 0.0))
    flat = remaining < MIN_QTY
    if flat and symbol == "BTCUSDT":
        st["entry"] = None
    unit = symbol_short(symbol)
    detail = (
        f"{symbol} 浮亏 {loss_points:.2f} ≥ {DISASTER_ATR:.0f}×ATR {stop_points:.2f}; "
        f"LIMIT 强平 {_chase_note(result)}; 剩余 {remaining:.4f} {unit}"
    )
    return {
        "action": "灾难止损平仓" if flat else "灾难止损部分平仓",
        "note": detail,
        "mark": mark,
        "flat": flat,
        "remaining": remaining,
        "result": result,
        "symbol": symbol,
    }


def _signed_qty(pos) -> float:
    pos_side = (getattr(pos, "side", "FLAT") or "FLAT").upper()
    qty = float(getattr(pos, "quantity", 0.0) or 0.0)
    if pos_side == "LONG":
        return qty
    if pos_side == "SHORT":
        return -qty
    return 0.0


def _fetch_symbol_bars(symbol: str, now: int):
    try:
        b15 = _closed_bars(fetch("15m", 400, symbol), SPEC_15M.interval_ms, now)
        b5 = _closed_bars(fetch("5m", 600, symbol), SPEC_5M.interval_ms, now)
        b1h = _closed_bars(fetch("1h", 300, symbol), 60 * 60 * 1000, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] 拉 K 线失败: {exc}")
        return None
    atr1h = atr_wilder(b1h["high"], b1h["low"], b1h["close"], 14)
    return {
        "15m": (b15, _align_atr(b15["ts"], SPEC_15M.interval_ms, b1h, atr1h)),
        "5m": (b5, _align_atr(b5["ts"], SPEC_5M.interval_ms, b1h, atr1h)),
    }


async def step(client: BinanceTestnetClient, st: dict, execute: bool) -> None:
    migrate_state(st)
    bal = await client.get_balance()
    now = int(time.time() * 1000)
    upnl = float(getattr(bal, "total_unrealized_pnl", 0.0) or 0.0)
    equity = float(bal.total_wallet_balance) + upnl

    day = now // 86_400_000
    mtm = equity
    st["peak"] = max(st.get("peak", 0.0) or 0.0, mtm)
    if st.get("day") != day:
        st["day"], st["day_start_eq"] = day, mtm
    dl = ((st.get("day_start_eq") or mtm) - mtm) / st["day_start_eq"] if st.get("day_start_eq") else 0.0
    dd = ((st.get("peak") or mtm) - mtm) / st["peak"] if st.get("peak") else 0.0
    if dd >= 0.10 and not st.get("halted"):
        st["halted"] = True
        print(f"[熔断] 累计回撤 {dd:.1%} ≥ 10% —— 停止开新仓, 等待人工指令")
    block = bool(st.get("halted")) or dl >= 0.03

    for symbol in TRADE_SYMBOLS:
        pos = await client.get_position(symbol)
        ex_side = _signed_qty(pos)
        entry_px = float(getattr(pos, "entry_price", 0.0) or 0.0)
        pack = _fetch_symbol_bars(symbol, now)
        if not pack:
            continue
        b15, atr15 = pack["15m"]
        latest15 = len(b15["ts"]) - 1
        tag15 = next((spec.tag for spec in specs_for_symbol(symbol)
                      if spec.interval == "15m"), ORDER_TAG)
        emergency = await disaster_limit_stop(
            client, st, ex_side=ex_side, entry_px=entry_px,
            atr_1h=float(atr15[latest15]) if latest15 >= 0 else float("nan"),
            execute=execute, symbol=symbol, tag=tag15,
        )
        if emergency:
            clear_symbol_books(st, symbol)
            if symbol == "BTCUSDT":
                st["entry"] = None
            if latest15 >= 0:
                log_row([_fmt(int(b15["ts"][latest15])),
                         f"{symbol_short(symbol)} {emergency['action']}",
                         "多" if ex_side > 0 else "空", f"{abs(ex_side):.4f}",
                         f"{emergency['mark']:.2f}", "", "", "",
                         f"{atr15[latest15]:.2f}", "", f"{equity:.2f}",
                         emergency["note"]])
            continue

        changed = False
        tag = tag15
        for spec in specs_for_symbol(symbol):
            book = st["strategies"][spec.id]
            bars, atr_al = pack[spec.interval]
            if len(bars["ts"]) == 0:
                continue
            before = json.dumps(book.get("entry"), sort_keys=True)
            did = process_strategy(
                spec, book, bars, atr_al,
                equity=equity, block=block, now=now, execute=execute,
            )
            after = json.dumps(book.get("entry"), sort_keys=True)
            if did or before != after:
                changed = True
                tag = spec.tag

        if changed or abs(desired_net(st, symbol) - ex_side) >= MIN_QTY:
            await sync_net(client, st, execute, symbol=symbol, tag=tag)

    # 旧面板仍读顶层 last_ts / entry，与 BTC 15m 账本对齐。
    s15 = st["strategies"]["kdj15"]
    st["last_ts"] = s15.get("last_ts")
    st["entry"] = s15.get("entry")
    st["missed_bars"] = s15.get("missed_bars", 0)
    st["missed_signals"] = s15.get("missed_signals", 0)
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
