"""人工干预检测 —— 让运行器不再把用户的平仓当成"偏差"补回去。

## 为什么需要这个模块

2026-10-02 19:47（北京时间），用户在币安网页手动平掉了 BTC 空 0.001 与
ETH 空 2.366。运行器在 **11 秒 / 35 秒**后把两笔原样开了回来，数量分毫不差。

原因不是信号，而是 `shadow/deploy.py` 的净仓同步：每 15 秒一轮，只要
「虚拟账本应有净额」与「交易所实际净额」差 ≥ `MIN_QTY` 就下差额单补回。
虚拟账本是运行器的"事实来源"，**人工操作被当成了需要修正的偏差**。

## 用户裁定（2026-10-02 20:27）

* 人工**减仓 / 平仓** → 该标的虚拟账本清零，**不补回**，等下一根信号再开仓
* 人工**加仓**       → 不吞进账本（那等于接受未经策略计算的额外风险），
                       **暂停该标的自动开仓并报警**，等人工确认后恢复

## 判定依据（可核验，不靠净值反推）

交易所 `userTrades` 不返回 `clientOrderId`，因此改用 `allOrders`：
委托号前缀属于本运行器（`kdj` / `kd5` / `e15` / `e5`）的是策略单，
其余（例如币安网页的 `web_`）一律视为外部干预。

净值比较**只用来判断方向**（敞口变大 = 加仓，否则 = 减仓），
不用来判断"是否发生了干预"——那由委托号前缀决定。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from shadow.strategy_books import clear_symbol_books

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "runtime", "shadow")

# 面板写入、运行器消费的「恢复」请求文件（一次性）。
RESUME_FILE = os.path.join(OUT, "external_resume.json")

# 本运行器自己的下单前缀（见 shadow/strategy_books.py 的 SPECS.tag）。
RUNNER_ORDER_PREFIXES = ("kdj", "kd5", "e15", "e5")

KIND_REDUCE = "reduce"
KIND_INCREASE = "increase"


def is_runner_order(row: Dict[str, Any]) -> bool:
    """这笔委托是不是本运行器下的（策略单）。"""
    cid = str(row.get("clientOrderId") or "").strip().lower()
    return cid.startswith(RUNNER_ORDER_PREFIXES)


def external_fills(
    orders: Sequence[Dict[str, Any]], *, since_ms: int
) -> List[Dict[str, Any]]:
    """挑出 `since_ms` 之后**成交过**且不是本运行器下的委托。

    只认真正有成交（`executedQty > 0`）的单：用户点了却没成交的挂单
    不改变净仓，不该触发"跟随人工"。
    """
    out: List[Dict[str, Any]] = []
    for row in orders or []:
        if is_runner_order(row):
            continue
        try:
            stamp = int(row.get("updateTime") or row.get("time") or 0)
        except (TypeError, ValueError):
            continue
        if stamp < int(since_ms):
            continue
        try:
            filled = float(row.get("executedQty") or 0)
        except (TypeError, ValueError):
            continue
        if filled <= 0:
            continue
        out.append(row)
    return out


def classify_external(
    ex_before: float, ex_after: float, *, min_qty: float
) -> Optional[str]:
    """按敞口变化判方向；变化小于最小变动单位则返回 None。"""
    if abs(float(ex_after) - float(ex_before)) < float(min_qty):
        return None
    if abs(float(ex_after)) > abs(float(ex_before)) + 1e-12:
        return KIND_INCREASE
    return KIND_REDUCE


def watch_for(st: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    return st.setdefault("external", {}).setdefault(symbol, {})


def apply_external(
    st: Dict[str, Any],
    symbol: str,
    kind: str,
    *,
    ex_before: float,
    ex_after: float,
    now: int,
    detail: str = "",
) -> Dict[str, Any]:
    """把一次人工干预落进状态：减仓清零账本，加仓暂停该标的。

    减仓走 `clear_symbol_books`（只清 `entry`，**保留 `last_ts`**）——
    这样不会把历史 K 线重放一遍导致立刻反手。
    """
    watch = watch_for(st, symbol)
    watch["last_kind"] = kind
    watch["detected_ms"] = int(now)
    watch["ex_before"] = float(ex_before)
    watch["ex_after"] = float(ex_after)
    watch["detail"] = detail

    if kind == KIND_REDUCE:
        clear_symbol_books(st, symbol)
        if symbol == "BTCUSDT":
            st["entry"] = None
        watch["hold"] = True          # 不补回，等下一根信号
        watch["paused"] = False
        watch["note"] = "人工减仓/平仓：已清零该标的策略账本，不补回，等下一根信号"
    else:
        watch["paused"] = True
        watch["paused_ms"] = int(now)
        watch["hold"] = False
        watch["note"] = "人工加仓：已暂停该标的自动开仓，等人工确认后恢复"
    return watch


def clear_hold_on_new_signal(st: Dict[str, Any], symbol: str) -> bool:
    """新信号改变了账本 → 解除"等下一根信号"的持有状态。

    返回是否真的解除了。
    """
    watch = (st.get("external") or {}).get(symbol)
    if not watch or not watch.get("hold"):
        return False
    watch["hold"] = False
    watch["note"] = "新信号已出现：恢复该标的净仓同步"
    return True


def consume_resume_requests(
    st: Dict[str, Any], *, path: Optional[str] = None
) -> List[str]:
    """消费面板写入的恢复请求；只接受**暂停之后**发出的请求。

    请求文件是一次性的：处理完即删除，避免重复解除。
    """
    target = path or RESUME_FILE
    if not os.path.exists(target):
        return []
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    reqs = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(reqs, dict):
        reqs = {}

    resumed: List[str] = []
    for symbol, req_ms in reqs.items():
        watch = (st.get("external") or {}).get(symbol)
        if not watch:
            continue
        try:
            req = int(req_ms)
        except (TypeError, ValueError):
            continue
        if req < int(watch.get("paused_ms") or 0):
            continue          # 早于暂停时刻的旧请求，不生效
        if watch.get("paused") or watch.get("hold"):
            watch["paused"] = False
            watch["hold"] = False
            watch["resumed_ms"] = req
            watch["note"] = "人工已确认恢复：该标的恢复自动开仓与净仓同步"
            resumed.append(symbol)
    try:
        os.remove(target)
    except OSError:
        pass
    return resumed


def request_resume(symbol: str, *, now: int, path: Optional[str] = None) -> int:
    """面板侧调用：写入一条恢复请求（返回请求时间戳）。"""
    target = path or RESUME_FILE
    os.makedirs(os.path.dirname(target), exist_ok=True)
    data: Dict[str, Any] = {"requests": {}}
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict) and isinstance(
                existing.get("requests"), dict
            ):
                data = existing
        except Exception:
            data = {"requests": {}}
    data["requests"][symbol] = int(now)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return int(now)


def summarize(st: Dict[str, Any]) -> Dict[str, Any]:
    """给面板用的摘要：哪些标的被人工干预、当前是否暂停/持有。"""
    out: Dict[str, Any] = {}
    for symbol, watch in (st.get("external") or {}).items():
        if not isinstance(watch, dict):
            continue
        paused = bool(watch.get("paused"))
        hold = bool(watch.get("hold"))
        if not (paused or hold or watch.get("detected_ms")):
            continue
        out[symbol] = {
            "paused": paused,
            "hold": hold,
            "last_kind": watch.get("last_kind"),
            "detected_ms": watch.get("detected_ms"),
            "ex_before": watch.get("ex_before"),
            "ex_after": watch.get("ex_after"),
            "note": watch.get("note") or "",
            "detail": watch.get("detail") or "",
        }
    return out
