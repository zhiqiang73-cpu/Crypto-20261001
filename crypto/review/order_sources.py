"""订单来源分类 —— 把「策略单」「功能测试单」「人工单」分开。

## 为什么需要这个模块

2026-10-02，用户看到测试网账户在 10:22 出现 5 笔成交，以为策略在那一分钟
产生了 5 次信号。交易所记录本身是真的，但把「策略自动交易」和「限价功能
测试」混在一张表里，就无法回答最基本的问题：**策略到底赚了多少**。

## 处理原则

交易所的历史**不能删除，也不该删除** —— 那会立刻变成用户最反感的「假数据」。
本模块只做一件事：**按可核验的依据给每笔委托打来源标签**，让页面能把
测试单与真实交易分开显示，且随时可切换查看全部。

## 分类依据（按优先级，先匹配先返回）

| 优先级 | 依据 | 判定 |
| --- | --- | --- |
| 1 | 委托号出现在策略台账 `runtime/shadow/deployed_orders.jsonl` | strategy |
| 2 | `clientOrderId` 以 `kdj` 开头 | strategy |
| 3 | `clientOrderId` 以 `web_` 开头 | manual_web |
| 4 | `clientOrderId` 以 `smk` / `smoke` 开头 | function_test |
| 5 | `clientOrderId` 以 `usr` 开头 | user_action |
| 6 | 本系统下单前缀 + 时间落在已记录的测试窗口内 | function_test |
| 7 | 其余 | unknown |

**未知来源一律保持可见。** 宁可多显示，也绝不因为猜不准而把真实成交藏起来。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
STRATEGY_LEDGER = os.path.join(ROOT, "runtime", "shadow", "deployed_orders.jsonl")

SOURCE_STRATEGY = "strategy"
SOURCE_MANUAL_WEB = "manual_web"
SOURCE_FUNCTION_TEST = "function_test"
SOURCE_USER_ACTION = "user_action"
SOURCE_UNKNOWN = "unknown"

SOURCE_LABELS = {
    SOURCE_STRATEGY: "策略自动",
    SOURCE_MANUAL_WEB: "网页手动",
    SOURCE_FUNCTION_TEST: "功能测试",
    SOURCE_USER_ACTION: "面板手动",
    SOURCE_UNKNOWN: "未判定",
}

# 只有这些来源会被页面默认隐藏（功能测试）。其余全部保留可见。
HIDDEN_BY_DEFAULT = (SOURCE_FUNCTION_TEST,)

# 本系统自己生成的下单前缀（见 trading/binance_client.py）。
OWN_PREFIXES = ("ps", "cx", "lmt", "mkt", "sl", "kdj", "smk", "usr")

# 已记录的测试窗口（北京时间 UTC+8，闭区间）。
# 每一个窗口都有对应的事故记录或验证记录，不是事后猜测：
#   * 2026-10-01 16:11–16:14 —— 首次冒烟：0.001 BTC 限价入场后立即平仓
#   * 2026-10-02 10:15–10:23 —— 限价追价 / post-only 费率验证
#     （该时段最近两根已收盘 K 线均无新交叉，运行器日志只有「观察」）
TEST_WINDOWS_BJ: Sequence[Tuple[str, str]] = (
    ("2026-10-01 16:11", "2026-10-01 16:14"),
    ("2026-10-02 10:15", "2026-10-02 10:23"),
)

_BJ = timezone(timedelta(hours=8))


def _bj_stamp(ms: Any) -> Optional[str]:
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=_BJ).strftime("%Y-%m-%d %H:%M")


def in_test_window(ms: Any) -> bool:
    """时间戳是否落在已记录的测试窗口内（北京时间，分钟精度）。"""
    stamp = _bj_stamp(ms)
    if stamp is None:
        return False
    return any(start <= stamp <= end for start, end in TEST_WINDOWS_BJ)


def load_strategy_order_ids(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """读取策略运行器落盘的委托台账（orderId -> 记录）。

    台账由 `shadow/deploy.py` 每次真实下单后追加。文件不存在时返回空字典，
    调用方**不得**因此把订单猜成策略单。
    """
    target = path or STRATEGY_LEDGER
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(target):
        return out
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                oid = str(rec.get("order_id") or "").strip()
                if oid:
                    out[oid] = rec
    except Exception:
        return out
    return out


def classify(
    row: Dict[str, Any],
    *,
    strategy_order_ids: Optional[Iterable[str]] = None,
    order_ids: Optional[Iterable[str]] = None,
) -> str:
    """给一笔委托或一笔成交判定来源。

    `row` 可以是 allOrders 的委托，也可以是 userTrades 的逐笔成交。
    `order_ids` 用于逐笔成交：其 orderId 所属委托已被判为某个来源时，
    成交沿用该来源。
    """
    order_id = str(row.get("orderId") or "").strip()
    client_id = str(row.get("clientOrderId") or "").strip()
    stamp_ms = row.get("time") or row.get("updateTime")

    known = set(str(x) for x in (strategy_order_ids or ()))
    if order_id and order_id in known:
        return SOURCE_STRATEGY
    if order_ids and order_id and order_id in set(str(x) for x in order_ids):
        return SOURCE_STRATEGY

    lowered = client_id.lower()
    if lowered.startswith("kdj"):
        return SOURCE_STRATEGY
    if lowered.startswith("web_"):
        return SOURCE_MANUAL_WEB
    if lowered.startswith(("smk", "smoke")):
        return SOURCE_FUNCTION_TEST
    if lowered.startswith("usr"):
        return SOURCE_USER_ACTION
    if lowered.startswith(OWN_PREFIXES) and in_test_window(stamp_ms):
        return SOURCE_FUNCTION_TEST
    return SOURCE_UNKNOWN


def annotate_orders(
    orders: Sequence[Dict[str, Any]],
    *,
    strategy_order_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """为委托列表补充 source 字段（不修改交易所原始字段）。"""
    known = set(str(x) for x in (strategy_order_ids or ()))
    out = []
    for row in orders:
        item = dict(row)
        item["source"] = classify(item, strategy_order_ids=known)
        item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
        out.append(item)
    return out


def annotate_trades(
    trades: Sequence[Dict[str, Any]],
    orders: Sequence[Dict[str, Any]],
    *,
    strategy_order_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """为逐笔成交补充 source：沿用其所属委托的来源，避免同一笔单两个口径。"""
    by_order = {str(o.get("orderId")): o for o in orders}
    strategy_orders = {
        str(o.get("orderId"))
        for o in orders
        if o.get("source") == SOURCE_STRATEGY
    }
    known = set(str(x) for x in (strategy_order_ids or ())) | strategy_orders
    out = []
    for row in trades:
        item = dict(row)
        parent = by_order.get(str(item.get("orderId")))
        if parent and parent.get("source"):
            item["source"] = parent["source"]
        else:
            item["source"] = classify(
                item,
                strategy_order_ids=known,
                order_ids=strategy_orders,
            )
        item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
        out.append(item)
    return out


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """按来源计数，供页面显示「已隐藏 N 笔测试单」。"""
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("source") or SOURCE_UNKNOWN)
        counts[key] = counts.get(key, 0) + 1
    return counts
