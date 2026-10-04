"""KDJ 交叉策略的币安合约测试网运行器。

BTCUSDT 与 ETHUSDT 各跑两条策略，按标的各记虚拟仓、只下该标的净额：
    15m: 当根收盘交叉且 MACD 能量柱方向一致（金叉+绿柱做多 / 死叉+红柱做空），
         下一根开盘下限价单，无 K 阈值；背离的交叉丢弃，不平仓也不反手
    5m:  金叉且 MACD 能量柱为正做多 / 死叉且能量柱为负做空，当根收盘即可
    5m 仓位：与同标的 15m 同向满仓，对着干则减半
    仓位 = 权益 × r ÷ (2 × ATR_1H), r=RISK_R, 向下取整 0.001
    开仓/平仓一律限价: post-only 贴盘口挂单争取 maker, 窗口耗尽才穿盘口兜底
    布林带仅记录 / 日亏与回撤门控可配置 / 10x 逐仓

只允许 Testnet; live 被 runtime_mode 闸门硬阻断。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import json
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from shadow.engine import (ATR_MULT_K, BREAK_EVEN_TRIGGER_ATR, DISASTER_ATR,
                           GATE_FEE_RATE, LEVERAGE, MIN_QTY, NORMAL_STOP_ATR,
                           RISK_R, floor_step)
from shadow.indicators import atr_wilder, boll, kdj, macd
from shadow.signals import (confirmed_signal, entry_signal, macd_gate,
                            macd_side, price_breaks)
from shadow.live import ENDPOINTS, MARKET, fetch, latest_mark_price, start_market_stream
from shadow.strategy_books import (SPEC_15M, SPEC_5M, SPECS, TRADE_SYMBOLS,
                                   apply_virtual_signal, clear_symbol_books,
                                   contra_5m_qty, desired_net, migrate_state,
                                   reconcile_symbol_books, reduce_only_for_delta,
                                   signal_reason, specs_for_symbol, layer_count,
                                   other_symbol_margin, symbol_short,
                                   trend_side)
from trading.cost_model import (net_break_even_price, stop_price_from_avg,
                               stop_is_tighter)
from trading.protective_orders import (ProtectionState,
                                       algo_ident, algo_trigger,
                                       cancel_algo_by_id,
                                       find_take_profit,
                                       place_take_profit,
                                       fetch_open_algo_orders,
                                       find_protective_stop,
                                       place_protective_stop,
                                       reconcile_protective,
                                       tighten_protective_stop)
from shadow.external_guard import (ExternalWatch, fill_blocked, note_net,
                                   scan_external)
from shadow.external_watch import (apply_external, classify_external,
                                   clear_hold_on_new_signal,
                                   consume_resume_requests, external_fills,
                                   watch_for)
from trading.binance_client import BinanceTestnetClient
from trading.models import OrderResult
from trading.runtime_mode import current_mode, validate_exchange_target
from config.market_endpoints import (MARKET_MAINNET, MarketMismatchError,
                                     assert_market_consistency,
                                     resolve_for_account)

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "runtime", "shadow")
TRADE_LOG = os.path.join(OUT, "deployed_trades.csv")
STATE = os.path.join(OUT, "deployed_state.json")
# 进程启动时刻（≈ 本模块导入时刻）。心跳据此区分「同一个进程跑了多久」
# 与「进程被重启过」——旧进程会一直报同一个 started_ms。
PROCESS_STARTED_MS = int(time.time() * 1000)
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

# 默认保留资金保护；测试网 launchd 显式关闭，以便连续采集故障样本。
BLOCK_ON_DAILY_LOSS = os.environ.get("BLOCK_ON_DAILY_LOSS", "1") != "0"
HALT_ON_MAX_DRAWDOWN = os.environ.get("HALT_ON_MAX_DRAWDOWN", "1") != "0"

COLS = ["时间", "动作", "方向", "数量", "价格", "净盈亏", "K", "D", "ATR_1H",
        "倍数", "权益", "说明"]

# 本运行器的 clientOrderId 前缀。没有它, 账户里策略单和测试单无法区分 ——
# 2026-10-02 用户看到 10:22 的 5 笔成交以为策略发了 5 次信号, 就是缺这个标签。
ORDER_TAG = "kdj"

# ---------------------------------------------------------------------------
# 交易所预挂保护单（2026-10-04 接入）
# ---------------------------------------------------------------------------
# 背景：此前保护完全在进程内 —— 进程一死、断网、关机，交易所上没有任何
# 保护单，仓位裸奔。实测确认 openAlgoOrders 返回空数组，与持仓同时存在。
#
# 接入后的分工：
#   * 交易所保护单是**主**保护：STOP_MARKET，closePosition=true，
#     workingType=MARK_PRICE。加层后自动跟随全部仓位，不需要重挂。
#   * 进程内的软件止损退化为**兜底**：只有在交易所保护单未被确认
#     （UNPROTECTED / UNKNOWN）时才真正下单，避免两边同时平仓。
#   * 保护单未确认时禁止开新仓 —— 宁可错过信号，不能裸奔。
EXCHANGE_STOPS_ENABLED = True

# 下单尝试的最小间隔（毫秒）。主循环 15 秒一轮，若验证环节因为字段解析或
# 限流持续失败，没有退避就会**每轮都下一张新单**，在交易所堆出一串重复的
# 保护单 —— 触发时重复平仓。只在真正需要下单时才受这个间隔约束；
# 「查到已有合格保护单」的正常路径不受影响。
# ---------------------------------------------------------------------------
# 分批止盈 + 剩余仓位移动止损（2026-10-04 加入）
# ---------------------------------------------------------------------------
# 用户文档第 6 行原本排除止盈；2026-10-04 19:45 用户明确要求「本次也给我
# 加入分批止盈」，故按方案 D 实施：到 +2×ATR 平掉一半，剩余转移动止损。
#
# 分批表：(ATR_1H 倍数, 平掉「当前仓位」的比例)。按顺序、由近及远。
# 比例是相对**当前**仓位，所以 ((2,0.5),(3,0.5)) 最终留下 25% 跟随。
TP_ENABLED = True
TP_STAGES: tuple = ((2.0, 0.5), (3.0, 0.5))

# 移动止损：止损与「入场以来最有利标记价」的间距（ATR_1H 倍数）。
# 只向保护利润的方向移动，永不放宽。1.0 比初始止损的 1.5 更紧 ——
# 这是刻意的：追踪的目的就是用一部分回撤空间换利润锁定。
TRAIL_ENABLED = True
TRAIL_ATR = 1.0

# 止盈/止损单的下单退避（共用）
PROTECTIVE_RETRY_INTERVAL_MS = 30_000
_PROTECTIVE_ATTEMPT: dict = {}


def _protective_attempt_ok(symbol: str) -> bool:
    """距上次下单尝试是否已过退避间隔。"""
    last = float(_PROTECTIVE_ATTEMPT.get(symbol) or 0.0)
    return (time.time() * 1000 - last) >= PROTECTIVE_RETRY_INTERVAL_MS

# 人工干预观察状态。不能放进 st：save_state 会 json.dump 整个 st，
# ExternalWatch 不是 JSON 可序列化的。进程重启后重建（首次扫描只记基线）。
_EXTERNAL_WATCHES: dict = {}


def _ext_watch(symbol: str) -> ExternalWatch:
    ew = _EXTERNAL_WATCHES.get(symbol)
    if ew is None:
        ew = ExternalWatch()
        _EXTERNAL_WATCHES[symbol] = ew
    return ew


def protection_state(st: dict, symbol: str) -> dict:
    return (st.get("protection") or {}).get(symbol) or {}


def protection_blocked(st: dict, symbol: str) -> Optional[str]:
    """保护单未确认时返回拦截原因，用于禁止开新仓。"""
    ps = protection_state(st, symbol)
    if ps.get("state") in ("PROTECTED",):
        return None
    if ps.get("state") in ("UNPROTECTED", "UNKNOWN"):
        return f"保护单未确认（{ps.get('state')}）：{ps.get('note', '')}"
    return None


def set_protection_state(st: dict, symbol: str, *, state: str, note: str,
                         algo_id: str = '', trigger: float = 0.0) -> None:
    """写入保护状态。**合并而非替换** —— best_price / tp_filled / tp_orders
    等字段由止盈与追踪逻辑维护，替换会把它们抹掉。"""
    rec = st.setdefault("protection", {}).setdefault(symbol, {})
    rec.update({
        "state": state, "note": note, "algo_id": algo_id,
        "trigger": float(trigger or 0.0), "ts": int(time.time() * 1000),
    })

# 补记窗口: 运行器停机后最多回看多少根 15m K 线 (96 根 = 24 小时)。
# 2026-10-02 用户在图表上看到 09:45 金叉, 而日志里那一根只有「观察」——
# 信号判定本身没错 (当时规则仍要求 K<30, 该根 K=50.73 不满足),
# 但「运行器停了多久、跳过了哪几根」当时完全无从查起。
MISSED_LOOKBACK_BARS = 96

# 净仓同步的死区。步长取整会让账本与交易所偶尔差一个最小步长（如 ETH 9.567 vs
# 9.566）；若拿它当"需要下单"，就会为一笔几 USDT 的微单发起 180 秒追价，而交易所
# 又会以最小名义额(20U)拒掉 —— 纯空转。留 2 个步长的死区。
SYNC_MIN_DELTA = MIN_QTY * 2.0

# 单笔保证金预算（占权益比例）。风险定量 qty = 权益×RISK_R/(2×ATR_1H) 只回答
# "想下多少"，不回答"下不下得起" —— 10× 逐仓下它常要求单仓占用 60~90% 权益的
# 保证金（2026-10-04 实测 BTC 需 86%、ETH 需 59%，合计 145%），两个标的在数学上
# 不可能同时持仓。这里按"该标的整仓"封顶，使两标的合计 ≤ 2×该值。
# 单标的保证金预算（占权益比例）。设 0 表示不封顶。
MARGIN_BUDGET_PER_TRADE = float(os.environ.get("MARGIN_BUDGET_PER_TRADE", "0.70"))

# 组合级保证金预算（BTC+ETH 合计，占权益比例）。设 0 表示不封顶。
#
# 2026-10-04：单标的 70% × 两个标的 = 140% > 可用余额，两标的都会互相挤爆
# （实测同时满 3 层需 5,570 USDT / 5,000 权益 = 111%）。改为组合级 80% 后：
#   只做一个标的 → 该标的可用到 min(70%, 80%) = 70%，BTC 满 3 层需 68% ⇒ 放得下
#   两个标的都做 → 先建仓的先占额度，后一个只能用剩下的，不会超出可用余额
# 先到先得，不做事后重分配：运行器每 tick 顺序处理标的，先处理的先占。
PORTFOLIO_MARGIN_BUDGET = float(os.environ.get("PORTFOLIO_MARGIN_BUDGET", "0.80"))

# 本运行器日志的轮转上限。launchd 用 StandardOutPath 把 stdout 重定向到文件且是
# O_APPEND, 所以重命名/替换都不起作用(我们的 fd 仍指向旧 inode, 之后的输出会写进
# 已被换走的文件而丢失)。只能"读末尾 → 就地截断重写": O_APPEND 保证 fd 的后续写入
# 仍落在文件末尾。
# 2026-10-04 之前从不轮转, trader.out 里混着 1268 条仓库搬家前的旧路径错误,
# 排查真实故障时噪音极大。
LOG_ROTATE_MAX_BYTES = 8 * 1024 * 1024
LOG_ROTATE_KEEP_LINES = 3000
LOG_FILES = ("com.crypto.btcusdt.testnet.trader.out.log",
             "com.crypto.btcusdt.testnet.trader.err.log")


def rotate_own_logs() -> None:
    """超限时就地轮转本运行器的 launchd 日志(保留末尾若干行)。失败不影响交易。"""
    for name in LOG_FILES:
        path = os.path.join(ROOT, "runtime", "logs", name)
        try:
            if not os.path.exists(path):
                continue
            if os.path.getsize(path) <= LOG_ROTATE_MAX_BYTES:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-LOG_ROTATE_KEEP_LINES:]
            if not tail:
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"[日志轮转 {datetime.now():%Y-%m-%d %H:%M:%S}] "
                         f"超过 {LOG_ROTATE_MAX_BYTES // (1024 * 1024)}MB, "
                         f"仅保留最后 {LOG_ROTATE_KEEP_LINES} 行\n")
                fh.writelines(tail)
            print(f"[日志轮转] {name} 已裁剪, 保留最后 {len(tail)} 行")
        except Exception as exc:  # noqa: BLE001  轮转是维护动作, 绝不中断交易
            print(f"[日志轮转] {name} 失败(已忽略): {exc}")


_SKIP_NOTE: dict = {}


def should_report_skip(symbol: str, key: str, now_ms: int, *,
                       interval_ms: int = 30 * 60 * 1000) -> bool:
    """同一个「开不出来」的情形不要每 tick 都刷日志。

    2026-10-04: 保证金不足会持续存在（直到减仓或换信号），若每 15 秒打印一次，
    一天就能刷出上万行把真正的事件淹掉。只在情形变化时、或每 30 分钟重报一次。
    """
    prev = _SKIP_NOTE.get(symbol)
    if prev and prev[0] == key and now_ms - prev[1] < interval_ms:
        return False
    _SKIP_NOTE[symbol] = (key, now_ms)
    return True


def apply_margin_budget(book: dict, qty: float, *, side: int, equity: float,
                        px: float, budget: Optional[float] = None,
                        others_margin: float = 0.0,
                        portfolio_budget: Optional[float] = None) -> tuple:
    """按保证金预算给数量封顶。返回 (数量, 说明)。

    两层预算，取更紧的一层作为「该标的整仓」上限：

      1. 单标的预算：equity × budget
      2. 组合预算：equity × portfolio_budget − 其它标的已占用（先到先得）

    同向加层时"整仓"= 已持有 + 本层，所以先扣掉已有量；反向或空仓时按整仓算
    （反手会先清掉旧层，不该被旧仓占掉额度）。
    """
    budget = MARGIN_BUDGET_PER_TRADE if budget is None else float(budget)
    if qty <= 0 or px <= 0 or equity <= 0:
        return qty, ""
    cap_total = (equity * budget * float(LEVERAGE) / px
                 if budget > 0 else float("inf"))
    port_note = ""
    pb = (PORTFOLIO_MARGIN_BUDGET if portfolio_budget is None
          else float(portfolio_budget))
    if pb > 0:
        room_usdt = equity * pb - float(others_margin or 0.0)
        if room_usdt <= 0:
            return 0.0, (f"组合保证金已满(合计≤{equity * pb:.2f}USDT; "
                         f"其它标的已占 {float(others_margin or 0.0):.2f})")
        room_qty = floor_step(room_usdt * float(LEVERAGE) / px)
        if room_qty < cap_total:
            cap_total = room_qty
            port_note = (f"组合预算封顶(合计≤{equity * pb:.2f}USDT; "
                         f"其它标的已占 {float(others_margin or 0.0):.2f})")
    if cap_total == float("inf"):
        return qty, ""
    entry = book.get("entry") or {}
    try:
        existing = abs(float(entry.get("qty") or 0))
        existing_side = int(entry.get("side") or 0)
    except (TypeError, ValueError):
        existing, existing_side = 0.0, 0
    room = (cap_total - existing) if (existing_side and side == existing_side) \
        else cap_total
    room = floor_step(max(0.0, room))
    if qty <= room:
        return qty, ""
    note = f"保证金预算封顶(整仓≤{cap_total:.4f})"
    return room, f"{port_note}; {note}" if port_note else note


def symbol_book_has_position(st: dict, symbol: str) -> bool:
    """该标的的虚拟账本里是否还记着仓位（用于识别「幽灵仓位」）。"""
    return any(
        float(((st["strategies"][spec.id].get("entry") or {}).get("qty")) or 0) > 0
        for spec in specs_for_symbol(symbol)
    )


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
    note = (f"{tag} 被动{meta.get('passive_attempts', 0)}次 "
            f"成交价={r.avg_price:.2f} 总单数={meta.get('steps_used', 0)}")
    # 失败时附上真实原因；2026-10-03 备注曾把「保证金不足 -2019」挡在外面。
    err = getattr(r, "error", "") or ""
    if not getattr(r, "ok", True) and err and err not in note:
        note = f"{note}; {err}"
    return note


def log_row(row: list) -> None:
    """追加一行到交易台账；必要时先补表头。

    ⚠ 表头判据必须是「文件不存在 **或 size==0**」。
    2026-10-04：系统重置脚本留下了 0 字节的空文件，只判 exists 就不会写表头，
    于是全部记录都没有列名，所有按列名读该 CSV 的读者（scripts/monitor_shadow.sh、
    review/panel_server.py、review/chart_data.py）一起抛 KeyError，监控每小时崩一次。
    """
    os.makedirs(OUT, exist_ok=True)
    try:
        need_header = os.path.getsize(TRADE_LOG) == 0
    except OSError:
        need_header = True          # 不存在 / 不可读 → 当作空
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if need_header:
            w.writerow(COLS)
        w.writerow(row)


def margin_skip_row(*, now_ms: int, symbol: str, ex: float, mark: float,
                    msg: str) -> list:
    """「保证金不足跳过」的台账行。

    必须严格 12 列、与 COLS 一一对齐：此前少了一列，msg 落到「权益」列，
    线上 monitor.log 出现 权益=保证金不足，跳过开仓...。
    """
    return [_fmt(now_ms),
            f"{symbol_short(symbol)} 保证金不足跳过",
            "多" if ex > 0 else ("空" if ex < 0 else "空仓"),
            f"{abs(ex):.4f}", f"{mark:.2f}",
            "", "", "",          # 净盈亏 / K / D
            "", "", "",          # ATR_1H / 倍数 / 权益（跳过时没有权益可填）
            msg]                 # 说明


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


def runtime_params() -> dict:
    """本进程**实际加载**的关键参数快照。

    2026-10-04：RISK_R 被改成 0.01 之后，从心跳/面板完全看不出线上进程其实还
    加载着旧的 0.03（编辑不热加载）。把这些值写进心跳，比对一眼即可发现
    「进程跑的配置 ≠ 磁盘上的配置」。
    """
    return {
        "risk_r": RISK_R,
        "margin_budget_per_trade": MARGIN_BUDGET_PER_TRADE,
        "portfolio_margin_budget": PORTFOLIO_MARGIN_BUDGET,
        "layer_risk_r": {spec.id: list(spec.layer_risk_r or ()) for spec in SPECS},
        "leverage": LEVERAGE,
        "atr_mult_k": ATR_MULT_K,
        "disaster_atr": DISASTER_ATR,
        "block_on_daily_loss": BLOCK_ON_DAILY_LOSS,
        "halt_on_max_drawdown": HALT_ON_MAX_DRAWDOWN,
        "max_layers": {spec.id: spec.max_layers for spec in SPECS},
    }


def params_fingerprint(params: dict) -> str:
    """参数快照的短指纹；同样的配置得到同样的指纹。"""
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def save_heartbeat(*, status: str, execute: bool, detail: str = "") -> None:
    """原子写入运行器心跳，供面板判断是否因进程退出而暂停。"""
    os.makedirs(OUT, exist_ok=True)
    params = runtime_params()
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
        # 进程身份与「实际加载的参数」——用来分辨旧进程仍在跑旧配置。
        "pid": os.getpid(),
        "started_ms": PROCESS_STARTED_MS,
        "params": params,
        "params_fingerprint": params_fingerprint(params),
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
                        signal_rule: str = "当根收盘交叉，下一根开盘下限价单；不使用 K 极值过滤",
                        k_long_max=None, k_short_min=None,
                        path: Optional[str] = None,
                        symbol: str = "BTCUSDT",
                        confirm_next: bool = False,
                        require_break: bool = False,
                        require_macd: bool = False) -> None:
    """写入当前最新已收盘 K 线的读数，不依赖它是不是「新 K 线」。

    即使运行器重启时这根 K 线已经被处理过，也要刷新快照；否则进程崩溃在
    JSON 写入前会造成「日志是新的、页面读数却是旧的」的假象。
    """
    px = float(c[i])
    sig_long = sig_short = gold = dead = False
    confirm_note = ""
    break_note = ""
    macd_note = ""
    dif_series, dea_series, hist_series = macd(c)
    hist_now = float(hist_series[i]) if i < len(hist_series) else float("nan")
    if confirm_next and i >= 2:
        sig_long, sig_short, gold, dead, confirm_note = confirmed_signal(
            k[i - 2], d[i - 2], k[i - 1], d[i - 1], k[i], d[i],
            k_long_max=k_long_max, k_short_min=k_short_min,
        )
    elif i > 0:
        sig_long, sig_short, gold, dead = entry_signal(
            k[i - 1], d[i - 1], k[i], d[i],
            k_long_max=k_long_max, k_short_min=k_short_min,
        )
        if require_break and (sig_long or sig_short):
            if not price_breaks(
                float(c[i]), float(h[i - 1]), float(l[i - 1]), float(atr_al[i]),
                want=(1 if sig_long else -1),
            ):
                sig_long = sig_short = False
                break_note = (
                    "死叉但未向下突破上一根低点" if dead
                    else "金叉但未向上突破上一根高点"
                )
    if require_macd:
        sig_long, sig_short, macd_note = macd_gate(sig_long, sig_short, hist_now)
    band_width = float(up[i] - lb[i])
    mult = (band_width / 2.0) / (GATE_FEE_RATE * px) if px > 0 else 0.0

    def finite(v):
        value = float(v)
        return value if np.isfinite(value) else None

    start = max(0, i + 1 - 36)
    series_k = [finite(k[j]) for j in range(start, i + 1)]
    series_d = [finite(d[j]) for j in range(start, i + 1)]
    series_hist = [finite(hist_series[j]) for j in range(start, i + 1)]

    rec = {
        "bar_utc": _fmt(int(ts[i])),
        "bar_ms": int(ts[i]),
        "open": finite(o[i]), "high": finite(h[i]),
        "low": finite(l[i]), "close": finite(px),
        "K": finite(k[i]), "D": finite(d[i]),
        "ATR_1H": finite(atr_al[i]),
        "mult": finite(mult),
        # MACD(12,26,9): 闸门只认 HIST 正负, DIF/DEA 一并记录便于核对。
        "MACD_DIF": finite(dif_series[i]) if i < len(dif_series) else None,
        "MACD_DEA": finite(dea_series[i]) if i < len(dea_series) else None,
        "MACD_HIST": finite(hist_now),
        "macd_side": macd_side(hist_now),
        # `crossing()` 接收 NumPy 标量时会返回 numpy.bool_；json 不认识它。
        # 若不显式转换, save_reading 会静默失败、面板永远显示旧 K 线。
        "signal_long": bool(sig_long), "signal_short": bool(sig_short),
        "gold": bool(gold), "dead": bool(dead),
        "confirm_next": bool(confirm_next),
        "confirm_note": confirm_note,
        "require_break": bool(require_break),
        "break_note": break_note,
        "require_macd": bool(require_macd),
        "macd_note": macd_note,
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
        "series": {"k": series_k, "d": series_d, "hist": series_hist},
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
                   symbol: str, tag: str, force_cross: bool = False,
                   available_balance: Optional[float] = None,
                   on_step=None):
    """把同一标的两条策略的虚拟仓合成净仓，只下差额。"""
    pos = await client.get_position(symbol)
    pos_side = (getattr(pos, "side", "FLAT") or "FLAT").upper()
    ex = float(getattr(pos, "quantity", 0.0) or 0.0) * (
        1.0 if pos_side == "LONG" else (-1.0 if pos_side == "SHORT" else 0.0))
    desired = desired_net(st, symbol)
    delta = desired - ex
    if abs(delta) < SYNC_MIN_DELTA:
        return None
    if not execute:
        print(f"[{symbol} 净仓观察] 应有 {desired:.4f} 实际 {ex:.4f} 差额 {delta:.4f}")
        return None
    side = "LONG" if delta > 0 else "SHORT"
    reduce_only = reduce_only_for_delta(ex, desired, MIN_QTY)

    # ---- 保证金可行性（2026-10-04）------------------------------------------
    # 背景：定量 = 权益 × RISK_R / (2 × ATR_1H)。BTC 的 price/ATR ≈ 426，于是
    # 名义 ≈ 6.4 × 权益；10× 逐仓下**单仓就要 ~64% 保证金**，两个标的在数学上
    # 不可能同时持仓。此前从不看可用保证金，于是每 tick 都发一笔注定被
    # -2019 拒掉的单（单 tick 被动挂 25+ 次），既空转又把整个 tick 拖垮。
    # 现在：加仓方向先算所需保证金，不足就直接不发单（账本随即回滚）。
    if not reduce_only and not force_cross:
        if available_balance is None:
            try:
                bal = await client.get_balance()
                available_balance = float(
                    getattr(bal, "available_balance", 0.0) or 0.0)
            except Exception as exc:  # noqa: BLE001  读不到就不拦，交给交易所兜底
                print(f"[{symbol} 净仓] 可用保证金读取失败，跳过校验: {exc}")
                available_balance = None
        if available_balance is not None:
            try:
                mark = float(await client.mark_price(symbol))
            except Exception:  # noqa: BLE001
                mark = 0.0
            if mark > 0:
                need = abs(delta) * mark / float(LEVERAGE)
                if need > float(available_balance):
                    msg = (f"保证金不足，跳过开仓：需 {need:.2f} > 可用 "
                           f"{float(available_balance):.2f}"
                           f"（目标 {desired:+.4f}，差额 {delta:+.4f}，"
                           f"{LEVERAGE}x 逐仓）")
                    now_ms = int(time.time() * 1000)
                    if should_report_skip(symbol, f"{desired:+.4f}|{side}", now_ms):
                        print(f"[{symbol} 净仓跳过] {msg}")
                        log_row(margin_skip_row(now_ms=now_ms, symbol=symbol,
                                                ex=ex, mark=mark, msg=msg))
                    return OrderResult(
                        ok=False, symbol=symbol,
                        side="BUY" if delta > 0 else "SELL",
                        position_side=side.upper(),
                        quantity=0.0, requested_qty=abs(delta),
                        cum_filled_qty=0.0, avg_price=0.0,
                        order_state="skipped_margin",
                        error=f"skipped_margin_insufficient: {msg}",
                    )

    result = await client.place_limit_chase(
        side=side, quantity=abs(delta), reduce_only=reduce_only,
        tag=tag, force_cross=force_cross, symbol=symbol, on_step=on_step,
    )
    record_order(result, action=f"{symbol_short(symbol)}净仓{side}")
    after = await client.get_position(symbol)
    after_side = (getattr(after, "side", "FLAT") or "FLAT").upper()
    after_qty = float(getattr(after, "quantity", 0.0) or 0.0) * (
        1.0 if after_side == "LONG" else (-1.0 if after_side == "SHORT" else 0.0))
    print(f"[{symbol} 净仓] 目标 {desired:.4f} 原 {ex:.4f} → {after_qty:.4f} "
          f"{_chase_note(result) or result.error}")
    # 把「实际达成的净仓」带回给调用方 —— 部分成交时账本要据此对齐。
    _raw = result.raw if isinstance(result.raw, dict) else {}
    result.raw = {**_raw, "net_after": after_qty,
                  "entry_after": float(getattr(after, "entry_price", 0.0) or 0.0)}
    return result


def process_strategy(spec, book: dict, bars: dict, atr_al, *,
                     equity: float, block: bool, now: int, execute: bool,
                     trend: int = 0, others_margin: float = 0.0) -> bool:
    """处理一条策略的已收盘 K 线，只改虚拟账本。返回是否需要同步净仓。"""
    ts = bars["ts"]
    o, h, l, c = (bars[x] for x in ("open", "high", "low", "close"))
    k, d, _j = kdj(h, l, c)
    _dif, _dea, hist = macd(c)
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
                confirm_next=spec.confirm_next,
                require_break=spec.require_break,
                require_macd=spec.require_macd,
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
                confirm_next=spec.confirm_next,
                require_break=spec.require_break,
                require_macd=spec.require_macd,
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
                if spec.require_macd:
                    gold, dead, _ = macd_gate(
                        gold, dead,
                        float(hist[j]) if j < len(hist) else float("nan"),
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
        if sigs:
            # 2026-10-04: 补记只留痕不下单；漏掉的若是「有效反向」，实盘仓位会与
            # 规则长期相反（当晚 ETH 漏掉两次反手，空单一直持到值班结束）。
            # 显式告警，避免只能靠事后人工翻日志才发现。
            book["missed_signal_last_ms"] = max(
                int(book.get("missed_signal_last_ms", 0) or 0),
                int(ts[missed[-1]]),
            )
            print(f"[{mark} ⚠️ 漏信号] 停机期间有 {sigs} 根交叉未执行 —— "
                  f"实盘仓位可能与规则相反，请人工核对账本与交易所")

    if i <= 0 or np.isnan(atr_al[i]) or (spec.confirm_next and i < 2):
        book["last_ts"] = int(ts[i])
        return False

    px = float(c[i])
    confirm_note = ""
    if spec.confirm_next:
        sig_long, sig_short, gold, dead, confirm_note = confirmed_signal(
            k[i - 2], d[i - 2], k[i - 1], d[i - 1], k[i], d[i],
            k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
        )
    else:
        sig_long, sig_short, gold, dead = entry_signal(
            k[i - 1], d[i - 1], k[i], d[i],
            k_long_max=spec.k_long_max, k_short_min=spec.k_short_min,
        )
    break_note = ""
    if spec.require_break and (sig_long or sig_short):
        want_brk = 1 if sig_long else -1
        if not price_breaks(
            float(c[i]), float(h[i - 1]), float(l[i - 1]), float(atr_al[i]),
            want=want_brk,
        ):
            sig_long = sig_short = False
            break_note = (
                "死叉但未向下突破上一根低点" if dead
                else "金叉但未向上突破上一根高点"
            )
    macd_note = ""
    if spec.require_macd:
        sig_long, sig_short, macd_note = macd_gate(
            sig_long, sig_short,
            float(hist[i]) if i < len(hist) else float("nan"),
        )
    want = 1 if sig_long else (-1 if sig_short else 0)
    qty = 0.0
    if not np.isnan(atr_al[i]) and atr_al[i] > 0:
        # 首层 1%，第2/3层各 0.5%；5m 等未配置分层风险的策略继续沿用 RISK_R。
        current_layers = layer_count(book.get("entry"))
        current_side = int((book.get("entry") or {}).get("side") or 0)
        layer_no = current_layers + 1 if want and current_side == want else 1
        risk_schedule = spec.layer_risk_r or ()
        layer_r = (float(risk_schedule[layer_no - 1])
                   if 0 < layer_no <= len(risk_schedule) else RISK_R)
        qty = floor_step(equity * layer_r / (ATR_MULT_K * float(atr_al[i])))
    qty, size_note = contra_5m_qty(
        qty, interval=spec.interval, want=want, trend=trend,
    )
    qty, cap_note = apply_margin_budget(
        book, qty, side=want, equity=equity, px=px,
        budget=MARGIN_BUDGET_PER_TRADE, others_margin=others_margin,
    )
    if cap_note:
        size_note = f"{size_note}; {cap_note}" if size_note else cap_note
    reason_gold = bool(sig_long) if spec.confirm_next else gold
    reason_dead = bool(sig_short) if spec.confirm_next else dead
    action, note, changed = apply_virtual_signal(
        book, sig_long=sig_long, sig_short=sig_short, qty=qty,
        px=px, atr=float(atr_al[i]), ms=int(ts[i]), block=block,
        min_qty=MIN_QTY, max_layers=spec.max_layers,
        meta={
            "interval": spec.interval,
            "strategy_id": spec.id,
            "reason": signal_reason(
                spec, gold=reason_gold, dead=reason_dead, k=float(k[i]),
            ),
            "k": float(k[i]),
            "d": float(d[i]),
            "signal": "golden_cross" if reason_gold else (
                "dead_cross" if reason_dead else ""),
            "symbol": spec.symbol,
        },
    )
    if spec.confirm_next and confirm_note and confirm_note != "观察":
        if action is None:
            note = confirm_note
        elif confirm_note not in (note or ""):
            note = f"{note}; {confirm_note}" if note else confirm_note
    elif action is None and macd_note:
        note = macd_note
    elif action is None and break_note:
        note = break_note
    elif action is None and (gold or dead) and not (sig_long or sig_short):
        note = f"交叉但不满足 K 阈值 (K={k[i]:.2f})"
    elif size_note:
        note = f"{note}; {size_note}" if note else size_note
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
        confirm_next=spec.confirm_next,
        require_break=spec.require_break,
        require_macd=spec.require_macd,
    )
    book["last_ts"] = int(ts[i])
    return changed


def book_for_symbol(st: dict, symbol: str) -> Optional[dict]:
    """该标的的策略账本（当前每个标的只有一条 15m 策略）。"""
    for spec in specs_for_symbol(symbol):
        return st.setdefault("strategies", {}).get(spec.id)
    return None


async def _protective_target(client: BinanceTestnetClient, st: dict, *,
                            symbol: str, ex_side: float, entry_px: float,
                            atr_1h: float):
    """算出该标的此刻应该挂在哪里的保护触发价。

    未到保本档 → 入场均价 ∓ 1.5×ATR_1H（需求 B 的「按实际持仓均价」）。
    已到保本档 → 净保本价（扣掉全部成本后预计不亏的位置，需求 C）。

    返回 (触发价, 是否保本档, tick)。触发价 <= 0 表示算不出来。
    """
    side = 1 if ex_side > 0 else -1
    tick = await client.price_tick(symbol)
    book = book_for_symbol(st, symbol) or {}
    entry = book.get("entry") or {}
    entry_ms = int(entry.get("ms") or 0)
    armed = (entry_ms > 0
             and int(book.get("break_even_armed_ms") or 0) == entry_ms)
    if armed:
        # 入场手续费与已发生资金费在账本里没有分项字段，按 taker 费率保守
        # 估算入场费（宁可把保本价算高一点，也不要把止损放到成本线以下）。
        be, _why = net_break_even_price(
            avg_price=float(entry_px), side=side, quantity=abs(ex_side),
            entry_fee_paid=0.0, funding_paid=0.0, tick=tick,
            entry_fee_missing=True,
        )
        target = be
        # 移动止损：止损与「入场以来最有利标记价」保持 TRAIL_ATR×ATR_1H。
        # 只朝保护利润的方向取更紧的一侧，永不放宽。
        if TRAIL_ENABLED:
            best = float(protection_state(st, symbol).get("best_price") or 0.0)
            if best > 0:
                trail = (best - TRAIL_ATR * float(atr_1h) if side > 0
                         else best + TRAIL_ATR * float(atr_1h))
                if trail > 0:
                    if side > 0:
                        target = max(target, trail) if target > 0 else trail
                    else:
                        target = min(target, trail) if target > 0 else trail
        if target > 0:
            return target, True, tick
    px = stop_price_from_avg(
        avg_price=float(entry_px), side=side, atr_1h=float(atr_1h),
        multiple=NORMAL_STOP_ATR, tick=tick,
    )
    return px, False, tick


async def manage_exchange_stop(client: BinanceTestnetClient, st: dict, *,
                               symbol: str, ex_side: float, entry_px: float,
                               atr_1h: float, execute: bool) -> Optional[dict]:
    """确保交易所有一张覆盖全仓的保护单；保本激活后收紧到净保本价。

    只在 execute 模式动作。返回 None 表示无需动作或已受保护；
    返回字典表示本轮做了什么（用于日志）。
    """
    if not EXCHANGE_STOPS_ENABLED or not execute:
        return None
    if abs(ex_side) < MIN_QTY or entry_px <= 0:
        return None
    if not (atr_1h > 0) or not np.isfinite(float(atr_1h)):
        return None
    side = 1 if ex_side > 0 else -1

    try:
        target, armed, _tick = await _protective_target(
            client, st, symbol=symbol, ex_side=ex_side, entry_px=entry_px,
            atr_1h=atr_1h)
    except Exception as exc:  # noqa: BLE001
        set_protection_state(st, symbol, state="UNKNOWN",
                             note=f"触发价计算失败: {exc}")
        return {"action": "保护单计算失败", "note": str(exc)}

    if target <= 0:
        set_protection_state(st, symbol, state="UNKNOWN",
                             note="触发价算不出来（均价或 ATR 非法）")
        return None

    # 记录入场以来最有利的标记价，供移动止损使用。必须在下单判断之前更新，
    # 否则止损会滞后一个 tick 才跟上新高。
    mark = float(latest_mark_price(symbol) or 0.0)
    if mark > 0:
        rec0 = protection_state(st, symbol)
        best0 = float(rec0.get("best_price") or 0.0)
        if best0 <= 0:
            rec0["best_price"] = mark
        elif side > 0:
            rec0["best_price"] = max(best0, mark)
        else:
            rec0["best_price"] = min(best0, mark)

    book = book_for_symbol(st, symbol)
    prev = dict((book or {}).get("exchange_stop") or {})
    prev_id = str(prev.get("algo_id") or "")

    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001 查不到 ≠ 没有，按未确认处理
        set_protection_state(st, symbol, state="UNKNOWN",
                             note=f"openAlgoOrders 查询失败: {exc}")
        return {"action": "保护单查询失败", "note": str(exc)}

    existing = find_protective_stop(orders, side=side, required_trigger=target)
    if existing is not None:
        aid = algo_ident(existing)
        trig = algo_trigger(existing)
        if book is not None:
            book["exchange_stop"] = {"algo_id": aid, "trigger": trig,
                                     "armed": armed}
        set_protection_state(st, symbol, state="PROTECTED",
                             note=("交易所保护单有效（保本档）" if armed
                                   else "交易所保护单有效（初始档）"),
                             algo_id=aid, trigger=trig)
        return None

    # 需要下单。已有旧单则先立后破；没有则首次建立。
    if not _protective_attempt_ok(symbol):
        # 退避期内不再下单。返回当前已知状态，让上层按未确认处理（禁止开新仓），
        # 但不在交易所堆重复保护单。
        ps = protection_state(st, symbol)
        set_protection_state(st, symbol, state=ps.get("state") or "UNKNOWN",
                             note=f"{ps.get('note', '')}（{PROTECTIVE_RETRY_INTERVAL_MS // 1000} 秒内不重试）",
                             algo_id=ps.get("algo_id", ""),
                             trigger=float(ps.get("trigger") or 0.0))
        return None
    _PROTECTIVE_ATTEMPT[symbol] = time.time() * 1000
    if prev_id:
        res = await tighten_protective_stop(
            client, symbol=symbol, side=side, new_trigger=target,
            old_algo_id=prev_id, quantity=abs(ex_side), close_position=True)
        verb = "收紧"
    else:
        res = await place_protective_stop(
            client, symbol=symbol, side=side, trigger_price=target,
            quantity=abs(ex_side), close_position=True)
        verb = "建立"

    if res.protects:
        if book is not None:
            book["exchange_stop"] = {"algo_id": res.algo_id,
                                     "trigger": res.trigger_price,
                                     "armed": armed}
        set_protection_state(
            st, symbol, state="PROTECTED",
            note=(f"{verb}保护单成功，触发价 {res.trigger_price:.2f}"
                  + (f"；旧单撤销未确认 {res.error}" if res.error else "")),
            algo_id=res.algo_id, trigger=res.trigger_price)
        detail = (f"{symbol} {verb}交易所保护单：{('多' if side > 0 else '空')} "
                  f"触发价 {res.trigger_price:.2f}（{abs(ex_side):.4f}）"
                  f"{'，已由交易所接管止损' if not res.error else ''}")
        print(f"[{symbol} 保护单] {detail}")
        log_row([_fmt(0), f"{symbol_short(symbol)} 保护单{verb}",
                 "多" if side > 0 else "空", f"{abs(ex_side):.4f}",
                 f"{res.trigger_price:.2f}", "", "", "", "", "",
                 "", detail])
        return {"action": f"保护单{verb}", "note": detail,
                "trigger": res.trigger_price, "state": "PROTECTED"}

    # 未确认：按未保护处理，并禁止开新仓
    set_protection_state(st, symbol, state=res.state.value,
                         note=f"{verb}未确认: {res.error}")
    print(f"[{symbol} ⚠️ 保护单{verb}未确认] {res.error}；"
          f"该标的暂停开新仓，软件止损继续兜底")
    return {"action": f"保护单{verb}未确认", "note": res.error,
            "state": res.state.value}


async def clear_ledger_if_stop_fired(client: BinanceTestnetClient, st: dict, *,
                                     symbol: str, ex_side: float) -> bool:
    """交易所已空仓、但账本还记着仓位时，判断是不是保护单打掉的，是就清账本。

    必要性：保护单由交易所触发成交，运行器不是下单方。若不处理，账本仍记着
    多头，净仓同步会认为「目标有仓、实际空仓」，把仓位**补回来** —— 正是要
    杜绝的行为。

    判定必须具体：只有「账本里记的那张保护单已不在 openAlgoOrders 里」才认定
    是保护单成交。查不到就**不清**，宁可下一轮再判，也不误清账本。
    """
    if abs(ex_side) >= MIN_QTY:
        return False
    book = book_for_symbol(st, symbol)
    if not book or not book.get("entry"):
        return False
    # 止损单与所有止盈单都算「我们自己的单」。仓位归零时，只要还有任何一张
    # 在挂，就说明不是被它们打掉的（可能是人工平仓），不擅自清账本。
    ours = {str((book.get("exchange_stop") or {}).get("algo_id") or "")}
    for t in (protection_state(st, symbol).get("tp_orders") or []):
        ours.add(str(t.get("algo_id") or ""))
    ours.discard("")
    if not ours:
        return False
    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol} 保护单成交判定] 查询失败，本轮不清账本: {exc}")
        return False
    if any(algo_ident(o) in ours for o in orders):
        return False        # 我们的单还在，仓位是别的原因变空的 → 不擅自清
    rec = dict(book.get("exchange_stop") or {})
    trig = float(rec.get("trigger") or 0.0)
    clear_symbol_books(st, symbol)
    if symbol == "BTCUSDT":
        st["entry"] = None
    set_protection_state(st, symbol, state="PROTECTED",
                         note=f"保护单已触发成交（触发价 {trig:.2f}），账本已清零")
    print(f"[{symbol} 保护单已成交] 触发价 {trig:.2f}；账本清零，不补回，等下一根信号")
    log_row([_fmt(0), f"{symbol_short(symbol)} 保护单成交",
             "", "", f"{trig:.2f}", "", "", "", "", "", "",
             f"{symbol} 交易所保护单触发平仓，账本清零"])
    return True


async def manage_take_profit(client: BinanceTestnetClient, st: dict, *,
                             symbol: str, ex_side: float, entry_px: float,
                             atr_1h: float, execute: bool) -> Optional[dict]:
    """按 TP_STAGES 逐批挂出止盈单（部分数量 + reduceOnly）。

    每批只挂「当前还没触发的那一批」，触发过的批次记在 tp_filled 里不再重挂。
    成交由 reconcile_tp_fills 认定，不在本函数里猜。
    """
    if not TP_ENABLED or not execute:
        return None
    if abs(ex_side) < MIN_QTY or entry_px <= 0 or not (atr_1h > 0):
        return None
    if not np.isfinite(float(atr_1h)):
        return None
    side = 1 if ex_side > 0 else -1
    rec = protection_state(st, symbol)
    idx = int(rec.get("tp_filled") or 0)
    if idx >= len(TP_STAGES):
        return None
    mult, frac = TP_STAGES[idx]

    tick = await client.price_tick(symbol)
    level = float(entry_px) + side * float(mult) * float(atr_1h)
    level = client._price_precision(level, tick)
    if level <= 0:
        return None
    qty = abs(ex_side) * float(frac)
    if qty < MIN_QTY:
        return None

    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol} 止盈] 查询失败，本轮不动作: {exc}")
        return None

    existing = find_take_profit(orders, side=side, trigger=level,
                                tolerance=max(tick, 0.0))
    if existing is not None:
        tp = rec.setdefault("tp_orders", [])
        aid = algo_ident(existing)
        if not any(str(t.get("algo_id")) == aid for t in tp):
            tp.append({"algo_id": aid, "trigger": algo_trigger(existing),
                       "qty": qty, "stage": idx, "filled": False})
        return None

    if not _protective_attempt_ok(f"{symbol}:tp"):
        return None
    _PROTECTIVE_ATTEMPT[f"{symbol}:tp"] = time.time() * 1000

    res = await place_take_profit(
        client, symbol=symbol, side=side, trigger_price=level, quantity=qty)
    if not res.protects:
        print(f"[{symbol} ⚠️ 止盈挂单未确认] 第{idx + 1}批 {level:.2f} × {qty}: "
              f"{res.error}")
        return {"action": "止盈挂单未确认", "note": res.error}

    rec.setdefault("tp_orders", []).append({
        "algo_id": res.algo_id, "trigger": res.trigger_price,
        "qty": qty, "stage": idx, "filled": False,
    })
    detail = (f"{symbol} 第{idx + 1}批止盈挂出：{('多' if side > 0 else '空')} "
              f"触发价 {res.trigger_price:.2f}（{mult}×ATR）"
              f" 数量 {qty:.4f}/{abs(ex_side):.4f}")
    print(f"[{symbol} 止盈] {detail}")
    log_row([_fmt(0), f"{symbol_short(symbol)} 止盈第{idx + 1}批",
             "多" if side > 0 else "空", f"{qty:.4f}",
             f"{res.trigger_price:.2f}", "", "", "", "", "", "", detail])
    return {"action": f"止盈第{idx + 1}批", "note": detail}


async def reconcile_tp_fills(client: BinanceTestnetClient, st: dict, *,
                             symbol: str, ex_side: float) -> bool:
    """认定止盈成交，并把账本对齐到交易所实际净仓。

    必要性：止盈单由**交易所**触发成交，运行器不是下单方。若不处理，账本仍
    记着原仓位，净仓同步会认为「目标有仓、实际少了一半」，把刚止盈掉的部分
    **买回来** —— 止盈就白做了。

    判定保持保守：只有「账本里记的那张止盈单已不在 openAlgoOrders 里」才认。
    无论是成交还是被撤，**交易所净仓才是事实**，所以一律按实际净仓对齐；
    对齐是幂等的，净仓没变就不会有任何改动。
    """
    rec = protection_state(st, symbol)
    tps = rec.get("tp_orders") or []
    pending = [t for t in tps if not t.get("filled")]
    if not pending:
        return False
    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol} 止盈成交判定] 查询失败，本轮不动账本: {exc}")
        return False
    alive = {algo_ident(o) for o in orders}
    gone = [t for t in pending if str(t.get("algo_id")) not in alive]
    if not gone:
        return False

    for t in gone:
        t["filled"] = True
        rec["tp_filled"] = max(int(rec.get("tp_filled") or 0),
                               int(t.get("stage") or 0) + 1)
    note = reconcile_symbol_books(st, symbol, float(ex_side))
    detail = (f"{symbol} 止盈第{gone[0].get('stage', 0) + 1}批已成交"
              f"（触发价 {float(gone[0].get('trigger') or 0):.2f}）；"
              f"账本对齐到交易所净仓 {float(ex_side):+.4f}"
              + (f"：{note}" if note else ""))
    print(f"[{symbol} 止盈成交] {detail}")
    log_row([_fmt(0), f"{symbol_short(symbol)} 止盈成交",
             "", f"{abs(float(ex_side)):.4f}",
             f"{float(gone[0].get('trigger') or 0):.2f}", "", "", "", "", "",
             "", detail])
    return True


async def reconcile_protection(client: BinanceTestnetClient, st: dict, *,
                               execute: bool) -> dict:
    """启动/重启时的保护单对账：有仓没保护就立刻补，补不上就禁止开新仓。

    需求 D：进程启动时以交易所实仓 + openAlgoOrders 为唯一事实来源对账。
    """
    summary = {}
    for symbol in TRADE_SYMBOLS:
        try:
            pos = await client.get_position(symbol)
            ex_side = _signed_qty(pos)
            entry_px = float(getattr(pos, "entry_price", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            set_protection_state(st, symbol, state="UNKNOWN",
                                 note=f"持仓查询失败: {exc}")
            summary[symbol] = {"state": "UNKNOWN", "note": str(exc)}
            print(f"[{symbol} 启动对账] 持仓查询失败: {exc}")
            continue

        if abs(ex_side) < MIN_QTY:
            # 空仓：清理残留保护单（上一笔的不能留到下一笔）
            try:
                res = await reconcile_protective(
                    client, symbol=symbol, side=0, quantity=0.0,
                    required_trigger=0.0)
                set_protection_state(st, symbol, state="PROTECTED",
                                     note=res.action)
                summary[symbol] = {"state": "FLAT", "note": res.action}
                print(f"[{symbol} 启动对账] 空仓；{res.action}")
            except Exception as exc:  # noqa: BLE001
                set_protection_state(st, symbol, state="UNKNOWN",
                                     note=f"残留保护单清理失败: {exc}")
                summary[symbol] = {"state": "UNKNOWN", "note": str(exc)}
            continue

        # 有仓：算出应挂的触发价，查交易所是否已有合格保护单
        atr_1h = float("nan")
        try:
            pack = _fetch_symbol_bars(symbol, int(time.time() * 1000))
            if pack:
                b15, atr_al = pack["15m"]
                i = len(b15["ts"]) - 1
                if i >= 0:
                    atr_1h = float(atr_al[i])
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol} 启动对账] ATR 取数失败: {exc}")

        side = 1 if ex_side > 0 else -1
        if not (atr_1h > 0) or not np.isfinite(atr_1h):
            set_protection_state(st, symbol, state="UNKNOWN",
                                 note="ATR 不可用，无法确定保护触发价")
            summary[symbol] = {"state": "UNKNOWN", "note": "ATR 不可用"}
            print(f"[{symbol} 启动对账] ⚠️ ATR 不可用，无法建立保护单")
            continue

        target, armed, _tick = await _protective_target(
            client, st, symbol=symbol, ex_side=ex_side, entry_px=entry_px,
            atr_1h=atr_1h)
        try:
            res = await reconcile_protective(
                client, symbol=symbol, side=side, quantity=abs(ex_side),
                required_trigger=target)
        except Exception as exc:  # noqa: BLE001
            set_protection_state(st, symbol, state="UNKNOWN",
                                 note=f"对账失败: {exc}")
            summary[symbol] = {"state": "UNKNOWN", "note": str(exc)}
            continue

        if res.state == ProtectionState.PROTECTED:
            book = book_for_symbol(st, symbol)
            if book is not None and res.stop is not None:
                book["exchange_stop"] = {"algo_id": res.stop.algo_id,
                                         "trigger": res.stop.trigger_price,
                                         "armed": armed}
            set_protection_state(st, symbol, state="PROTECTED",
                                 note=f"{res.action}（触发价 "
                                      f"{res.stop.trigger_price if res.stop else 0:.2f}）",
                                 algo_id=(res.stop.algo_id if res.stop else ""),
                                 trigger=(res.stop.trigger_price if res.stop else 0.0))
            summary[symbol] = {"state": "PROTECTED",
                               "trigger": (res.stop.trigger_price if res.stop else 0.0)}
            print(f"[{symbol} 启动对账] 已受保护：{res.action}"
                  + (f"；触发价 {res.stop.trigger_price:.2f}" if res.stop else ""))
            continue

        # 没有合格保护单 → 立刻补一张（有仓裸奔是最高优先级）
        print(f"[{symbol} 启动对账] ⚠️ 有仓但无合格保护单，立即补挂")
        acted = await manage_exchange_stop(
            client, st, symbol=symbol, ex_side=ex_side, entry_px=entry_px,
            atr_1h=atr_1h, execute=execute)
        ps = protection_state(st, symbol)
        summary[symbol] = {"state": ps.get("state", "UNKNOWN"),
                           "note": ps.get("note", "")}
        if ps.get("state") != "PROTECTED":
            print(f"[{symbol} 启动对账] ⚠️ 保护单仍未确认，该标的禁止开新仓")
    return summary


async def protective_stop(client: BinanceTestnetClient, st: dict, *,
                          symbol: str, ex_side: float, entry_px: float,
                          atr_1h: float, mark: float, execute: bool,
                          tag: str = ORDER_TAG,
                          exchange_protected: bool = False) -> Optional[dict]:
    """正常止损（1.5×ATR_1H）与保本止损（浮盈 +1.5×ATR 后移到入场价）。

    与 disaster_limit_stop 的分工：本函数管更近的两档，它在更远处（3×ATR），
    所以先判本函数即可。三档都走同一种平仓方式：带滑点上限的穿盘口 **限价** 单
    （reduce_only，绝不发 MARKET）。

    保本状态用 `entry["ms"]`（开仓那根 K 线）绑定：存的是 `break_even_armed_ms`，
    只有它等于当前持仓的开仓 ms 才算已激活。这样反手/换仓后旧标记自动失效，
    不会把上一笔的保本状态带到新仓上。

    返回 None 表示未触发或不该动；返回字典表示已尝试平仓。
    """
    if not execute or ex_side == 0.0 or not np.isfinite(float(atr_1h)):
        return None
    if entry_px <= 0 or atr_1h <= 0 or mark <= 0:
        return None
    book = book_for_symbol(st, symbol)
    if not book:
        return None
    entry = book.get("entry") or {}
    entry_ms = int(entry.get("ms") or 0)
    if entry_ms <= 0:
        return None

    # 1) 保本止损激活：浮盈达到阈值 → 止损上移到入场价
    armed = int(book.get("break_even_armed_ms") or 0) == entry_ms
    if not armed:
        trigger = BREAK_EVEN_TRIGGER_ATR * float(atr_1h)
        reached = ((mark >= entry_px + trigger) if ex_side > 0
                   else (mark <= entry_px - trigger))
        if reached:
            book["break_even_armed_ms"] = entry_ms
            armed = True
            print(f"[{symbol} 保本止损已激活] 浮盈达 "
                  f"{BREAK_EVEN_TRIGGER_ATR}×ATR（{trigger:.2f}）；"
                  f"止损上移到入场价 {entry_px:.2f}")

    # 2) 判定触发：已激活 → 保本（距离 0）；否则 → 正常止损 1.5×ATR
    if armed:
        stop_px, why = entry_px, "保本止损"
    else:
        stop_px = (entry_px - NORMAL_STOP_ATR * float(atr_1h) if ex_side > 0
                   else entry_px + NORMAL_STOP_ATR * float(atr_1h))
        why = "正常止损"
    hit = (mark <= stop_px) if ex_side > 0 else (mark >= stop_px)
    if not hit:
        return None

    # 交易所保护单已确认时，平仓由交易所的 STOP_MARKET 完成。这里再发一张
    # 穿盘口限价单会和它抢同一笔仓位：先成交的那张平掉，后一张因 reduceOnly
    # 无仓被拒（不会反向开仓，但会白白多付一次 taker 手续费）。
    if exchange_protected:
        if not book.get("stop_defer_logged"):
            print(f"[{symbol} {why}已触发] 交易所保护单已接管平仓，"
                  f"软件止损不再重复下单")
            book["stop_defer_logged"] = True
        return None
    book.pop("stop_defer_logged", None)

    close_side = "SHORT" if ex_side > 0 else "LONG"
    result = await client.place_limit_chase(
        side=close_side, quantity=abs(ex_side), reduce_only=True,
        tag=tag, force_cross=True, symbol=symbol,
    )
    record_order(result, action=f"{symbol_short(symbol)}{why}平仓")
    after = await client.get_position(symbol)
    remaining = abs(float(getattr(after, "quantity", 0.0) or 0.0))
    flat = remaining < MIN_QTY
    if flat:
        clear_symbol_books(st, symbol)
        book.pop("break_even_armed_ms", None)
        if symbol == "BTCUSDT":
            st["entry"] = None
    detail = (f"{symbol} {why}触发：{('多头' if ex_side > 0 else '空头')} "
              f"入场 {entry_px:.2f} 止损 {stop_px:.2f}（{why}）"
              f"{'；已打平' if flat else f'；剩余 {remaining:.4f}'}")
    print(f"[{symbol} {why}] {detail}")
    return {
        "action": f"{why}平仓" if flat else f"{why}部分平仓",
        "note": detail, "mark": mark, "flat": flat,
        "remaining": remaining, "result": result, "symbol": symbol,
    }


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
    # 15m 信号只在收盘后变化；同一根K线不重复拉400根历史数据。
    # 5m 已退出实盘，避免为未启用策略继续消耗REST权重。
    slot15 = (now // (15 * 60 * 1000)) * (15 * 60 * 1000)
    slot1h = (now // (60 * 60 * 1000)) * (60 * 60 * 1000)
    cache = getattr(_fetch_symbol_bars, "_cache", {})
    old = cache.get(symbol)
    if old and old["slot15"] == slot15 and old["slot1h"] == slot1h:
        return old["pack"]
    try:
        b15 = _closed_bars(fetch("15m", 400, symbol), SPEC_15M.interval_ms, now)
        b1h = (_closed_bars(fetch("1h", 300, symbol), 60 * 60 * 1000, now)
               if not old or old["slot1h"] != slot1h else old["b1h"])
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] 拉 K 线失败: {exc}")
        return None
    atr1h = atr_wilder(b1h["high"], b1h["low"], b1h["close"], 14)
    pack = {
        "15m": (b15, _align_atr(b15["ts"], SPEC_15M.interval_ms, b1h, atr1h)),
        "5m": None,
    }
    cache[symbol] = {"slot15": slot15, "slot1h": slot1h, "b1h": b1h, "pack": pack}
    _fetch_symbol_bars._cache = cache
    return pack


EXTERNAL_ORDER_SCAN_LIMIT = 500


async def detect_external(
    client: BinanceTestnetClient, symbol: str, *, ex_now: float, now: int,
    watch: dict,
):
    """检测交易所侧是否发生了非本运行器造成的变化（人工下单）。

    判定只认「不是本运行器的委托、且真的成交了」——委托号前缀是硬证据，
    不靠净值反推。净值变化只用来定方向：敞口变大 = 加仓，否则 = 减仓。

    第一次见到该标的只记基线不判定，这样运行器重启后不会把「重启前就存在
    的仓位」误判成人工干预。
    """
    # 兼容字段：重启对账与既有测试依赖 last_check_ms / last_net 的精确值，
    # 继续按 tick 维护。它们**不再**用作扫描窗口与判定基线。
    # ⚠ 必须先读后写：下面要用上一 tick 的值给新观察状态做种子，写反了种子
    # 就变成当前值，等于把基线悄悄推进 —— 又回到「漏检」的老问题。
    legacy_net = watch.get("last_net")
    legacy_ms = int(watch.get("last_check_ms") or 0)
    watch["last_check_ms"] = int(now)
    watch["last_net"] = float(ex_now)

    # ⚠ 2026-10-04 修复：旧实现每个 tick 推进 last_check_ms/last_net，却每 60
    # 秒才扫一次订单，扫描执行时查询起点是「上一个 tick」而不是「上一次扫描」，
    # 中间约 45 秒的窗口永远扫不到 —— 用户在非扫描 tick 手工平仓时，系统会
    # 因为「没检出人工干预」而把仓位补回来。
    #
    # 现在扫描窗口 = 距上次扫描的全部时间，基线只在扫描时推进；并且净仓一变
    # 就立刻立 pending 闸门，禁止自动补仓，直到扫描给出原因。见 external_guard。
    ew = _ext_watch(symbol)
    if ew.scan_net is None and legacy_net is not None:
        # 进程重启后**续用**已落盘的基线，而不是盲目重新起算：停机期间发生的
        # 净仓变化（无论谁造成的）下一轮就能定性，不会因为「重新起算」而漏掉。
        # 若变化其实是本运行器自己的单子成交，扫描会认出委托号前缀并判为非
        # 人工干预，闸门随即解除，不会误伤。
        ew.scan_net = float(legacy_net)
        ew.last_scan_ms = int(watch.get("last_scan_ms") or legacy_ms or 0)
    note_net(ew, ex_now=ex_now, now=now, min_qty=MIN_QTY)
    watch["scan_pending"] = bool(ew.pending)
    hit = await scan_external(
        client, symbol, ex_now=ex_now, now=now, watch=ew, min_qty=MIN_QTY,
        classify=classify_external, fills_fn=external_fills,
    )
    watch["scan_pending"] = bool(ew.pending)
    watch["scan_reason"] = ew.last_reason
    if hit:
        return hit
    if ew.pending:
        print(f"[{symbol} 人工干预] 净仓变化未定性，已暂停自动补仓"
              f"（{ew.last_reason}）")
    return None


async def process_symbol(client: BinanceTestnetClient, st: dict, symbol: str, *,
                         execute: bool, block: bool, equity: float,
                         now: int) -> None:
    """处理单个标的的完整一轮（对照 / 人工干预 / 灾难止损 / 信号 / 净仓同步）。

    与 step 的分工：step 负责权益与闸门，本函数负责单标的。**异常一律向 step
    抛出**，由 step 隔离——一个标的失败不得影响另一个标的。

    2026-10-04 两处关键语义变更：
      1. 账本快照 + 回滚：虚拟账本只在「委托真的成交」后才保留改动，失败零成交
         则还原。此前 `apply_virtual_signal` 在委托**之前**就改账本，导致账本
         与交易所长期脱节（BTC 记着 0.675 多单，交易所始终空仓）。
      2. 灾难止损只有真的打平才清账本；部分成交/失败时账本保留。
    """
    pos = await client.get_position(symbol)
    ex_side = _signed_qty(pos)
    entry_px = float(getattr(pos, "entry_price", 0.0) or 0.0)
    # 人工干预检测：必须排在信号处理与净仓同步之前。
    # 2026-10-02 19:47 用户在网页手动平仓, 11/35 秒后被运行器原样补回 ——
    # 就是因为这里没有区分「谁动的仓」。
    watch = watch_for(st, symbol)
    hit = await detect_external(
        client, symbol, ex_now=ex_side, now=now, watch=watch
    )
    if hit and execute:
        watch = apply_external(
            st, symbol, hit["kind"],
            ex_before=hit["ex_before"], ex_after=hit["ex_after"],
            now=now, detail=hit["detail"],
        )
        label = "人工减仓跟随" if hit["kind"] == "reduce" else "人工加仓暂停"
        print(f"[{symbol} {label}] {hit['detail']}")
        log_row([_fmt(now), f"{symbol_short(symbol)} {label}",
                 "多" if hit["ex_after"] > 0 else "空",
                 f"{abs(hit['ex_after']):.4f}", "", "", "", "", "", "",
                 f"{equity:.2f}", hit["detail"]])
    pack = _fetch_symbol_bars(symbol, now)
    if not pack:
        return

    # 快照排在人工干预之后（apply_external 记的是已发生的事实，不该被回滚）。
    snap = {spec.id: copy.deepcopy(st["strategies"][spec.id].get("entry"))
            for spec in specs_for_symbol(symbol)}

    def rollback(reason: str) -> None:
        restored = False
        for sid, prev in snap.items():
            cur = st["strategies"][sid].get("entry")
            if json.dumps(cur, sort_keys=True) != json.dumps(prev, sort_keys=True):
                st["strategies"][sid]["entry"] = prev
                restored = True
        if restored:
            print(f"[{symbol} 账本回滚] {reason}；账本已还原为本轮处理前的状态")

    b15, atr15 = pack["15m"]
    latest15 = len(b15["ts"]) - 1
    atr_1h = float(atr15[latest15]) if latest15 >= 0 else float("nan")
    tag15 = next((spec.tag for spec in specs_for_symbol(symbol)
                  if spec.interval == "15m"), ORDER_TAG)

    async def safety_checkpoint(bid: float, ask: float) -> bool:
        """追价期间的安全检查点：刷新心跳 + 评估灾难止损，需要时中止追价。

        追价最长 180 秒且会占住主循环，这期间既不处理另一标的、也不评估止损。
        检查点复用追价**自己已经拿到的盘口价**，不额外发任何请求。

        只在「已持仓、本次是加仓/反手」时才有意义：空仓开首层时 ex_side==0，
        没有仓位需要保护。
        """
        save_heartbeat(status="running", execute=execute,
                       detail=f"{symbol} 追价中")
        if ex_side == 0.0 or entry_px <= 0 or not (atr_1h > 0) \
                or not np.isfinite(atr_1h):
            return False
        mid = (bid + ask) / 2.0
        loss = (entry_px - mid) if ex_side > 0 else (mid - entry_px)
        if loss >= DISASTER_ATR * atr_1h:
            print(f"[{symbol} ⚠️ 追价中止] 浮亏 {loss:.2f} ≥ "
                  f"{DISASTER_ATR:.0f}×ATR {DISASTER_ATR * atr_1h:.2f}；"
                  f"停止追价，立即交给灾难止损")
            return True
        return False

    # 止盈成交对账必须排在保护单管理之前：先把账本对齐到交易所实际净仓，
    # 后面的「缺保护单」判断和净仓同步才不会被虚高的账本带偏。
    if ex_side != 0.0 and TP_ENABLED and execute:
        try:
            await reconcile_tp_fills(client, st, symbol=symbol, ex_side=ex_side)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol} 止盈成交判定] 异常: {type(exc).__name__}: {exc}")

    # 交易所预挂保护单：先确保交易所有一张覆盖全仓的条件单。
    # 顺序必须在软件止损之前 —— 后面要按它是否确认来决定要不要软件兜底下单。
    ex_stop_state = ""
    if ex_side != 0.0 and EXCHANGE_STOPS_ENABLED and execute:
        try:
            await manage_exchange_stop(
                client, st, symbol=symbol, ex_side=ex_side,
                entry_px=entry_px, atr_1h=atr_1h, execute=execute)
            ex_stop_state = str(protection_state(st, symbol).get("state") or "")
        except Exception as exc:  # noqa: BLE001 保护单管理失败不得拖垮整个标的
            print(f"[{symbol} 保护单] 管理异常: {type(exc).__name__}: {exc}")
            ex_stop_state = "UNKNOWN"

    # 交易所保护单已成交 → 账本清零，绝不补回
    try:
        if await clear_ledger_if_stop_fired(client, st, symbol=symbol,
                                           ex_side=ex_side):
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol} 保护单成交判定] 异常: {exc}")

    # 分批止盈：按 TP_STAGES 逐批挂单
    if ex_side != 0.0 and TP_ENABLED and execute:
        try:
            await manage_take_profit(
                client, st, symbol=symbol, ex_side=ex_side,
                entry_px=entry_px, atr_1h=atr_1h, execute=execute)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol} 止盈] 管理异常: {type(exc).__name__}: {exc}")

    # 正常止损（1.5×ATR）/ 保本止损：比灾难止损近，必须先判。
    try:
        if ex_side == 0.0:
            mark_now = 0.0
        else:
            mark_now = float(latest_mark_price(symbol) or 0.0)
            if mark_now <= 0:
                mark_now = float(getattr(pos, "mark_price", 0.0) or 0.0)
            if mark_now <= 0:
                mark_now = float(await client.mark_price(symbol))
        stopped = await protective_stop(
            client, st, symbol=symbol, ex_side=ex_side, entry_px=entry_px,
            atr_1h=atr_1h, mark=mark_now, execute=execute, tag=tag15,
            exchange_protected=(ex_stop_state == "PROTECTED"),
        )
    except Exception:
        rollback("正常/保本止损调用失败")
        raise
    if stopped:
        if latest15 >= 0:
            log_row([_fmt(int(b15["ts"][latest15])),
                     f"{symbol_short(symbol)} {stopped['action']}",
                     "多" if ex_side > 0 else "空", f"{abs(ex_side):.4f}",
                     f"{stopped['mark']:.2f}", "", "", "",
                     f"{atr15[latest15]:.2f}", "", f"{equity:.2f}",
                     stopped["note"]])
        return

    try:
        emergency = await disaster_limit_stop(
            client, st, ex_side=ex_side, entry_px=entry_px,
            atr_1h=atr_1h, execute=execute, symbol=symbol, tag=tag15,
        )
    except Exception:
        rollback("灾难止损调用失败")
        raise
    if emergency:
        if emergency.get("flat"):
            clear_symbol_books(st, symbol)
            if symbol == "BTCUSDT":
                st["entry"] = None
        else:
            print(f"[{symbol} 灾难止损未打平] 剩余 {emergency.get('remaining')}；"
                  f"账本保持不变，下一轮继续尝试")
        if latest15 >= 0:
            log_row([_fmt(int(b15["ts"][latest15])),
                     f"{symbol_short(symbol)} {emergency['action']}",
                     "多" if ex_side > 0 else "空", f"{abs(ex_side):.4f}",
                     f"{emergency['mark']:.2f}", "", "", "",
                     f"{atr15[latest15]:.2f}", "", f"{equity:.2f}",
                     emergency["note"]])
        return

    changed = False
    tag = tag15
    try:
        for spec in specs_for_symbol(symbol):
            book = st["strategies"][spec.id]
            bars, atr_al = pack[spec.interval]
            if len(bars["ts"]) == 0:
                continue
            before = json.dumps(book.get("entry"), sort_keys=True)
            did = process_strategy(
                spec, book, bars, atr_al,
                equity=equity,
                block=(block or bool(watch.get("paused"))
                       or bool(protection_blocked(st, symbol))),
                now=now, execute=execute,
                trend=trend_side(st, symbol),
                # 组合级保证金预算：其它标的已占的额度先扣掉（先到先得）。
                others_margin=other_symbol_margin(st, symbol, float(LEVERAGE)),
            )
            after = json.dumps(book.get("entry"), sort_keys=True)
            if did or before != after:
                changed = True
                tag = spec.tag
    except Exception:
        rollback("信号处理失败")
        raise

    if changed:
        # 人工平仓后「等下一根信号」：账本被新信号改动即解除持有。
        clear_hold_on_new_signal(st, symbol)
    # 自动补仓闸门（需求 D）：
    #   1) 人工干预净仓变化未定性 → 禁止用 desired_net 自动补仓
    #   2) 保护单未确认 → 禁止开新仓（宁可错过信号，不能裸奔）
    allow_sync = (not watch.get("paused") and not watch.get("hold")
                  and not watch.get("scan_pending"))
    if allow_sync and (changed or
                       abs(desired_net(st, symbol) - ex_side) >= SYNC_MIN_DELTA):
        try:
            result = await sync_net(client, st, execute, symbol=symbol, tag=tag,
                                    on_step=safety_checkpoint)
        except Exception:
            rollback("净仓同步调用失败")
            raise
        if (result is not None and not getattr(result, "ok", False)
                and float(getattr(result, "cum_filled_qty", 0.0) or 0.0) <= 0):
            rollback(f"下单未成交: {result.error}")
            # 交易所侧该标的本来就空仓、委托又没成交 ⇒ 账本也必须归零。
            # 否则会长期留下一个「策略以为持有、交易所没有」的幽灵仓位：既污染面板
            # 与 desired_net，又让每 tick 都白跑一次净仓同步（2026-10-04 的 BTC
            # 空单 0.382 就是旧代码留下的幽灵仓）。口径与 apply_external 处理人工
            # 平仓一致：清零、不补回、等下一根信号。
            if abs(ex_side) < MIN_QTY and symbol_book_has_position(st, symbol):
                clear_symbol_books(st, symbol)
                if symbol == "BTCUSDT":
                    st["entry"] = None
                print(f"[{symbol} 幽灵仓位清零] 交易所空仓但账本有仓，"
                      f"已清零该标的账本，等下一根信号")
        elif result is not None:
            # 首笔成交一出现就补挂保护单（需求 B）——不能等下一个 tick，
            # 追价最长 180 秒，这段窗口里新仓位必须已经有保护。
            if ex_side != 0.0 or abs(desired_net(st, symbol)) >= MIN_QTY:
                try:
                    pos_after = await client.get_position(symbol)
                    side_after = _signed_qty(pos_after)
                    px_after = float(getattr(pos_after, "entry_price", 0.0) or 0.0)
                    if abs(side_after) >= MIN_QTY and px_after > 0:
                        await manage_exchange_stop(
                            client, st, symbol=symbol, ex_side=side_after,
                            entry_px=px_after, atr_1h=atr_1h, execute=execute)
                except Exception as exc:  # noqa: BLE001
                    print(f"[{symbol} 保护单] 成交后补挂异常: {exc}")
            # 部分成交对齐：委托可能只成交了一部分，账本不能继续记着没成交的部分。
            # 2026-10-04 ETH 目标 -15.854 实际只到 -7.430，账本却一直虚高。
            raw = getattr(result, "raw", None)
            net_after = raw.get("net_after") if isinstance(raw, dict) else None
            if net_after is not None:
                note = reconcile_symbol_books(st, symbol, float(net_after))
                if note:
                    print(f"[{symbol} 账本对齐] {note}")


async def step(client: BinanceTestnetClient, st: dict, execute: bool) -> None:
    migrate_state(st)
    # 面板的「确认恢复」请求：一次性消费，只接受暂停之后发出的。
    for _sym in consume_resume_requests(st):
        print(f"[{_sym} 人工恢复] 已解除暂停，恢复自动开仓与净仓同步")
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
    if not HALT_ON_MAX_DRAWDOWN:
        st["halted"] = False
    elif dd >= 0.10 and not st.get("halted"):
        st["halted"] = True
        print(f"[熔断] 累计回撤 {dd:.1%} ≥ 10% —— 停止开新仓, 等待人工指令")
    block = (
        (HALT_ON_MAX_DRAWDOWN and bool(st.get("halted")))
        or (BLOCK_ON_DAILY_LOSS and dl >= 0.03)
    )

    for symbol in TRADE_SYMBOLS:
        try:
            await process_symbol(
                client, st, symbol, execute=execute, block=block,
                equity=equity, now=now,
            )
        except Exception as exc:  # noqa: BLE001  单标的失败必须隔离
            # 2026-10-04: 此前任一标的下单失败都会抛穿整个 tick —— 另一标的被跳过、
            # 状态不落盘，实测造成连续数小时的服务降级。现在失败只限于本标的。
            print(f"[{symbol}] 本轮处理失败（已隔离，不影响其它标的）："
                  f"{type(exc).__name__}: {exc}")
            print(traceback.format_exc())
            save_heartbeat(status="degraded", execute=execute,
                           detail=f"{symbol} {type(exc).__name__}: {exc}")
        finally:
            # 每标的落盘：此前只在整轮结束后 save_state，任何一步失败整轮状态全丢。
            save_state(st)

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

    # ---- 启动保护单对账（需求 D）----------------------------------------
    # 以交易所实仓 + openAlgoOrders 为唯一事实来源。有仓没保护就立刻补挂；
    # 补不上就把该标的标成 UNPROTECTED，主循环会禁止它开新仓。
    if EXCHANGE_STOPS_ENABLED:
        try:
            _st0 = load_state()
            summary = await reconcile_protection(client, _st0,
                                                 execute=args.execute)
            save_state(_st0)
            print("启动保护单对账结果：")
            for _sym, _info in summary.items():
                print(f"  {_sym}: {_info}")
        except Exception as exc:  # noqa: BLE001 对账失败不阻断启动，但会记录
            print(f"[启动对账] 失败（不阻断启动，主循环仍会逐轮修复）："
                  f"{type(exc).__name__}: {exc}")
            print(traceback.format_exc())

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
    start_market_stream(TRADE_SYMBOLS)
    try:
        while True:
            try:
                rotate_own_logs()
                await step(client, st, args.execute)
                save_heartbeat(status="running", execute=args.execute)
            except Exception as exc:  # noqa: BLE001
                # 2026-10-04: 此前只打 str(exc)，而 TimeoutError 的消息是空串 ——
                # 日志里只剩 196 条 "异常: "，没有类型也没有堆栈，完全无法排障。
                print(f"[{datetime.now():%H:%M:%S}] 异常: "
                      f"{type(exc).__name__}: {exc}")
                print(traceback.format_exc())
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
