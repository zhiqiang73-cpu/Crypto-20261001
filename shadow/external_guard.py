"""人工干预检测：修掉「每 15 秒推进基线、每 60 秒才扫描」的时间窗漏洞。

原实现的缺陷（shadow/deploy.py::detect_external）
--------------------------------------------------
    last_ms = watch["last_check_ms"]      # 上个 tick 的时刻
    watch["last_check_ms"] = now          # ← 每个 tick 都推进
    watch["last_net"] = ex_now            # ← 每个 tick 都推进
    ...
    if now - watch["last_scan_ms"] < 60_000: return None   # 60 秒才扫一次
    orders = await client.all_orders(symbol, start_time=last_ms, ...)

主循环约 15 秒一个 tick，但扫描 60 秒才做一次。扫描真正执行时，查询起点是
**上一个 tick**（now−15s），不是上一次扫描（now−60s）。于是 [now−60s, now−15s]
这段窗口里的成交永远扫不到：

    T−45s  用户手工平仓
    T−30s  tick：净仓变了但扫描未到点 → 返回 None
    T−15s  tick：基线已被推进到「已平仓」状态 → 无变化可判
    T      tick：扫描执行，查询窗口只有 [T−15s, T] → 漏掉 T−45s 的手工单
           → 未检出人工干预 → 随后的净仓同步把仓位补回来

修复要点
--------
1. **扫描窗口 = 距上次扫描的全部时间**，不是距上个 tick。
2. **基线只在扫描时推进**；每个 tick 只做「净仓是否变了」的快速判定。
3. **变化未定性前禁止自动补仓**：净仓一变就先立 pending 闸门，扫描给出
   原因（本运行器的单 / 人工单）之后才解除。原因不明就一直拦着。
4. pending 一旦立起就**立刻扫描**，不等 60 秒周期 —— 闸门开着的时间越短越好。

本模块不依赖网络，只依赖传入的 client（需实现 all_orders）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCAN_INTERVAL_MS = 60_000        # 常规扫描周期（限流考虑）
SCAN_LOOKBACK_MARGIN_MS = 5_000  # 查询起点往前多留一点，覆盖下单/成交时间差
ORDER_SCAN_LIMIT = 500


@dataclass
class ExternalWatch:
    """单标的的人工干预观察状态。

    ⚠ last_scan_ms / scan_net 只能由扫描推进，绝不能在每个 tick 推进 ——
    那正是原实现漏检的根因。
    """
    last_scan_ms: int = 0
    scan_net: Optional[float] = None      # 上次扫描时的交易所净仓
    pending: bool = False                 # 净仓变了但原因未明 → 禁止自动补仓
    pending_since_ms: int = 0
    pending_from_net: float = 0.0
    last_reason: str = ""
    scans: int = 0
    misses: int = 0                       # 扫描失败次数（429 等）

    def as_dict(self) -> dict:
        return {
            "last_scan_ms": self.last_scan_ms,
            "scan_net": self.scan_net,
            "pending": self.pending,
            "pending_since_ms": self.pending_since_ms,
            "last_reason": self.last_reason,
            "scans": self.scans,
            "misses": self.misses,
        }


def note_net(watch: ExternalWatch, *, ex_now: float, now: int,
             min_qty: float = 1e-9) -> Optional[str]:
    """每个 tick 调用：净仓相对**上次扫描**的基线是否变了。

    变了就立刻立 pending 闸门。这一步只看数字，不发任何请求，所以每个 tick
    都可以跑 —— 这正是快速拦住「补回仓位」的关键。
    """
    if watch.scan_net is None:
        return None
    if abs(float(ex_now) - float(watch.scan_net)) <= min_qty:
        return None
    if not watch.pending:
        watch.pending = True
        watch.pending_since_ms = int(now)
        watch.pending_from_net = float(watch.scan_net)
    return (f"净仓 {watch.pending_from_net:+.4f} → {float(ex_now):+.4f}，"
            f"原因待查明")


def fill_blocked(watch: ExternalWatch) -> Optional[str]:
    """净仓同步（desired_net 自动补仓）是否被拦。

    需求原文：「在原因未查明前禁止用 desired_net 自动补仓。」
    返回非 None 即为拦截原因。
    """
    if not watch.pending:
        return None
    return (f"检测到净仓变化但尚未定性（自 {watch.pending_from_net:+.4f}），"
            f"暂停自动补仓")


def should_scan(watch: ExternalWatch, *, now: int) -> bool:
    """是否该扫描：到周期了，或者有未定性的变化（立刻扫，别等 60 秒）。"""
    if watch.scan_net is None:
        return True
    if watch.pending:
        return True
    return (int(now) - watch.last_scan_ms) >= SCAN_INTERVAL_MS


async def scan_external(
    client: Any, symbol: str, *, ex_now: float, now: int,
    watch: ExternalWatch, min_qty: float = 1e-9,
    classify=None, fills_fn=None,
) -> Optional[Dict[str, Any]]:
    """扫描交易所委托，判定本次净仓变化是不是人工干预造成的。

    返回 None 表示「无人工干预」或「还没到扫描时机」；
    返回字典表示已确认的人工干预（含 kind / 明细）。
    """
    if not should_scan(watch, now=now):
        return None

    if watch.scan_net is None:
        # 第一次见到该标的：只记基线，不判定 —— 否则重启后会把重启前就
        # 存在的仓位误判成人工干预。
        watch.last_scan_ms = int(now)
        watch.scan_net = float(ex_now)
        watch.pending = False
        return None

    ex_before = float(watch.scan_net)
    # ⚠ 查询窗口必须覆盖「距上次扫描的全部时间」，不是距上个 tick。
    window_start = max(0, int(watch.last_scan_ms) - SCAN_LOOKBACK_MARGIN_MS)

    try:
        orders = await client.all_orders(
            symbol, start_time=window_start, limit=ORDER_SCAN_LIMIT
        )
    except Exception as exc:  # noqa: BLE001 429/网络失败不得当成「没有干预」
        watch.misses += 1
        watch.last_reason = f"扫描失败: {exc}"
        logger.warning("[%s] 人工干预扫描失败: %s", symbol, exc)
        # 基线不推进：下次还要把这段窗口重新扫一遍，不能因为一次 429 就漏掉。
        return None

    watch.scans += 1
    watch.last_scan_ms = int(now)
    watch.scan_net = float(ex_now)
    watch.pending = False

    hits = (fills_fn or _default_external_fills)(
        orders or [], since_ms=window_start
    )
    if not hits:
        watch.last_reason = "无外部成交"
        return None

    kind = (classify or _default_classify)(
        ex_before, float(ex_now), min_qty=min_qty
    )
    if kind is None:
        watch.last_reason = "外部成交存在但净仓无有效变化"
        return None

    parts = "；".join(
        f"{o.get('side')} {o.get('executedQty')} @{o.get('avgPrice')} "
        f"[{str(o.get('clientOrderId') or '')[:14]}]"
        for o in hits[:4]
    )
    watch.last_reason = kind
    return {
        "kind": kind,
        "ex_before": ex_before,
        "ex_after": float(ex_now),
        "detail": (f"{symbol} 交易所净额 {ex_before:+.4f} → "
                   f"{float(ex_now):+.4f}；{parts}"),
    }


# ---------------------------------------------------------------------------
# 默认实现（与 deploy.py 现有语义保持一致，可在测试里替换）
# ---------------------------------------------------------------------------
def _default_external_fills(orders, *, since_ms: int):
    """只认「不是本运行器的委托、且真的成交了」。委托号前缀是硬证据。"""
    from shadow.deploy import external_fills  # 延迟导入，避免循环依赖
    return external_fills(orders, since_ms=since_ms)


def _default_classify(before: float, after: float, *, min_qty: float):
    from shadow.deploy import classify_external
    return classify_external(before, after, min_qty=min_qty)
