"""完整持仓周期净收益配对 — 把交易所逐笔成交还原成「一笔完整交易」。

问题背景（2026-10-04 审计）
--------------------------------------------------------------------------
面板此前把**逐笔成交腿**当成一笔交易来统计胜率与盈亏比。币安 USDⓈ-M 的
``userTrades`` 里只有平仓腿带 ``realizedPnl``，开仓腿 ``realizedPnl`` 为 0；
一次分层建仓会产生多笔开仓腿，一次平仓也可能被拆成多笔成交腿。于是：

* 一次分批平仓会被算成「多笔盈利交易」，胜率与盈亏比被系统性放大；
* 账户摘要此前只取每标的最近 200 笔成交，开仓腿落在窗口外时其手续费缺失，
  「单笔净边际」的分母（开仓名义）偏小，边际被高估。

本模块按**完整持仓周期**重新配对，并显式报告覆盖区间与截断情况，使
「每笔净收益」可以被交易所流水逐笔对账。

配对规则
--------------------------------------------------------------------------
1. 按 ``(time, id)`` 升序扫描逐笔成交，维护带符号持仓 ``pos``；
2. ``BUY`` 增加多头/减少空头，``SELL`` 增加空头/减少多头；
3. 一笔成交跨越零点（反手）时按数量比例拆成「平旧仓」与「开新仓」两部分，
   ``realizedPnl`` 只归属平仓部分，``commission`` 按数量比例分摊；
4. ``|pos|`` 回到容差内即结算成一个 :class:`Cycle`；
5. 数据末尾仍未平掉的持仓单列为未平仓，不计入已平仓统计；
6. 资金费按时间归属到当时持仓的那个周期，无法归属的记为孤儿资金费。

对冲模式（``positionSide`` 为 ``LONG``/``SHORT``）按持仓方向分组后各自配对。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 净仓容差：步长取整会让账本与交易所偶尔差一个最小步长，那种差值不构成
# 「还没平完」。与 shadow/deploy.py::SYNC_MIN_DELTA 同量级。
QTY_TOL = 1e-6

PAGE_LIMIT = 1000      # 币安 userTrades 单次上限
MAX_PAGES = 40         # 分页护栏：防止异常情况下无限翻页

# 币安硬性要求 userTrades / income 的 startTime 与 endTime 跨度不超过 7 天，
# 超出会直接报错。取 6 天留出安全边界，避免边界取整把跨度顶过 7 天。
WINDOW_MS = 6 * 86_400_000

# 同一次追价序列内的成交间隔在追价窗口（180 秒）内；不同层之间至少隔一根
# 15m K 线。取 300 秒把两者分开，留出重挂与兜底的余量。
LAYER_GAP_MS = 300_000


@dataclass
class Cycle:
    """一笔完整持仓周期（从空仓到再次空仓）。"""

    symbol: str
    side: int                  # +1 多 / -1 空
    open_ms: int = 0
    close_ms: int = 0
    open_qty: float = 0.0
    open_notional: float = 0.0
    gross: float = 0.0         # realizedPnl 合计
    commission: float = 0.0
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    funding: float = 0.0       # 带符号：负数为支付
    fills: int = 0
    open_orders: int = 0       # 层数：按追价序列分组，不是按交易所 orderId 数
    close_orders: int = 0
    closed: bool = False
    # 开仓/加仓用到的委托号，用于把周期归到策略单或人工单。
    open_order_ids: set = field(default_factory=set)
    # strategy | mixed | unattributed —— 判定依据是委托是否出现在运行器台账，
    # 台账里没有的**不猜**成策略单。
    source: str = "unattributed"

    @property
    def net(self) -> float:
        """扣手续费与资金费后的净额（USDT）。"""
        return self.gross - self.commission + self.funding

    @property
    def edge_bps(self) -> Optional[float]:
        """单笔净边际：净额 / 开仓名义金额（bp），不依赖复利。"""
        if not self.open_notional:
            return None
        return self.net / self.open_notional * 10000.0

    @property
    def hold_min(self) -> float:
        if not self.open_ms or not self.close_ms:
            return 0.0
        return (self.close_ms - self.open_ms) / 60000.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _signed_delta(fill: Dict[str, Any]) -> float:
    """成交腿对带符号持仓的增量：BUY 为正，SELL 为负。"""
    qty = abs(_num(fill.get("qty")))
    side = str(fill.get("side") or "").upper()
    return qty if side == "BUY" else -qty


def split_legs(pos: float, delta: float,
               tol: float = QTY_TOL) -> Tuple[List[Tuple[str, float]], float]:
    """把一笔带符号成交拆成若干 ``(动作, 带符号数量)`` 腿。

    动作取 ``"open"`` 或 ``"close"``。跨越零点时自然拆成「先平后开」两段，
    这就是反手（平掉累计层再按反向开新仓）在成交层面的样子。

    返回 ``(腿列表, 新持仓)``。
    """
    legs: List[Tuple[str, float]] = []
    cur = pos
    remaining = delta
    guard = 0
    while abs(remaining) > tol and guard < 8:
        guard += 1
        if abs(cur) <= tol:
            legs.append(("open", remaining))
            cur = remaining
            remaining = 0.0
        elif (cur > 0) == (remaining > 0):
            legs.append(("open", remaining))
            cur += remaining
            remaining = 0.0
        else:
            close_qty = min(abs(cur), abs(remaining))
            # 平仓腿的数量取**成交方向**（与当前持仓相反），这样 cur + qty
            # 才会把持仓推回零点；取持仓方向会把仓位越平越大。
            qty = close_qty if remaining > 0 else -close_qty
            legs.append(("close", qty))
            cur += qty
            remaining -= qty
            if abs(cur) <= tol:
                cur = 0.0
    return legs, cur


def _position_side_key(fill: Dict[str, Any]) -> str:
    """对冲模式下按持仓方向分组；单向模式（BOTH）统一成空键。"""
    raw = str(fill.get("positionSide") or "BOTH").upper()
    return "" if raw in ("", "BOTH") else raw


def pair_cycles(fills: Sequence[Dict[str, Any]], *,
                symbol: Optional[str] = None,
                tol: float = QTY_TOL) -> Tuple[List[Cycle], List[Cycle]]:
    """把逐笔成交配对成完整持仓周期。

    返回 ``(全部周期, 未平仓周期)``；已平仓的周期 ``closed=True``。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for fill in fills:
        if _signed_delta(fill) == 0:
            continue
        groups.setdefault(_position_side_key(fill), []).append(fill)

    cycles: List[Cycle] = []
    for key in sorted(groups):
        ordered = sorted(
            groups[key],
            key=lambda f: (int(_num(f.get("time"))), int(_num(f.get("id")))),
        )
        cycles.extend(_pair_group(ordered, symbol=symbol, tol=tol))

    cycles.sort(key=lambda c: (c.open_ms, c.close_ms))
    return cycles, [c for c in cycles if not c.closed]


def _pair_group(ordered: Sequence[Dict[str, Any]], *,
                symbol: Optional[str], tol: float) -> List[Cycle]:
    out: List[Cycle] = []
    pos = 0.0
    cur: Optional[Cycle] = None
    open_order_ids: set = set()
    close_order_ids: set = set()

    for fill in ordered:
        delta = _signed_delta(fill)
        legs, _ = split_legs(pos, delta, tol)
        if not legs:
            continue

        sym = str(fill.get("symbol") or symbol or "")
        t_ms = int(_num(fill.get("time")))
        price = _num(fill.get("price"))
        commission = _num(fill.get("commission"))
        realized = _num(fill.get("realizedPnl"))
        maker = bool(fill.get("maker"))
        order_id = str(fill.get("orderId") or "")

        total_qty = sum(abs(q) for _, q in legs) or 1.0
        close_qty_total = sum(abs(q) for a, q in legs if a == "close") or 0.0

        for action, qty in legs:
            share = abs(qty) / total_qty
            leg_fee = commission * share
            # realizedPnl 只归属平仓部分，按平仓腿数量比例分摊。
            leg_realized = (
                realized * (abs(qty) / close_qty_total) if close_qty_total else 0.0
            )

            if action == "open":
                if cur is None:
                    cur = Cycle(
                        symbol=sym,
                        side=1 if qty > 0 else -1,
                        open_ms=t_ms,
                        open_qty=0.0,
                    )
                    open_order_ids = set()
                    close_order_ids = set()
                    last_open_ms = None
                cur.open_qty += abs(qty)
                cur.open_notional += abs(qty) * price
                cur.fills += 1
                if order_id:
                    open_order_ids.add(order_id)
                # 层数按「一次追价序列」算，不能按交易所 orderId 数：一次追价会
                # 先后用多个 orderId（post-only 部分成交后重挂，再 IOC 兜底），
                # 按 orderId 数会把 1 层数成 3 层，进而高估加仓次数。
                if last_open_ms is None or t_ms - last_open_ms > LAYER_GAP_MS:
                    cur.open_orders += 1
                last_open_ms = t_ms
                cur.open_order_ids = set(open_order_ids)
                # 加仓后周期的最早建仓时间保持首次开仓时刻。
                if not cur.open_ms:
                    cur.open_ms = t_ms
                pos += qty
            else:  # close
                if cur is None:
                    # 数据窗口从半途开始：没有对应的开仓腿，无法构成完整周期，
                    # 记为未平仓周期之外的孤立平仓，交由覆盖度信息提示。
                    logger.debug("孤立平仓腿 %s @%s，跳过配对", sym, t_ms)
                    pos += qty
                    continue
                cur.gross += leg_realized
                cur.fills += 1
                if order_id:
                    close_order_ids.add(order_id)
                cur.close_orders = len(close_order_ids)
                cur.close_ms = t_ms
                pos += qty

            cur.commission += leg_fee
            if maker:
                cur.maker_fee += leg_fee
            else:
                cur.taker_fee += leg_fee

            # 关键顺序：平仓腿把持仓推回零点后必须**立刻**结算旧周期，
            # 否则同一笔反手成交里的开仓腿会被错误地并进旧周期。
            if action == "close" and abs(pos) <= tol:
                cur.closed = True
                out.append(cur)
                cur = None
                open_order_ids = set()
                close_order_ids = set()

    if cur is not None:
        cur.closed = False
        out.append(cur)
    return out


def attribute_funding(cycles: Iterable[Cycle],
                      income_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把资金费按时间归属到当时持仓的周期。

    币安 ``income`` 的 ``income`` 字段带符号：负数为支付。归属不上的记为
    孤儿资金费并在返回里单列，不静默丢弃。
    """
    cyc = [c for c in cycles]
    by_symbol: Dict[str, List[Cycle]] = {}
    for c in cyc:
        by_symbol.setdefault(c.symbol, []).append(c)

    orphan = 0.0
    orphan_rows = 0
    matched = 0.0
    for row in income_rows or []:
        amount = _num(row.get("income"))
        if amount == 0:
            continue
        sym = str(row.get("symbol") or "")
        t_ms = int(_num(row.get("time")))
        target: Optional[Cycle] = None
        for c in by_symbol.get(sym, ()):  # 同一标的周期互不重叠，首个命中即正确
            if c.open_ms <= t_ms <= (c.close_ms or t_ms):
                target = c
                break
        if target is None:
            orphan += amount
            orphan_rows += 1
        else:
            target.funding += amount
            matched += amount

    return {
        "matched": matched,
        "orphan": orphan,
        "orphan_rows": orphan_rows,
    }


def summarize(cycles: Sequence[Cycle], *, funding_orphan: float = 0.0,
              open_cycles: Sequence[Cycle] = ()) -> Dict[str, Any]:
    """按完整持仓周期汇总，输出字段名与面板既有口径保持兼容。

    ``cycles`` 应只包含**窗口内已平仓**的周期；未平仓周期单独用
    ``open_cycles`` 传入，避免把「还开着的仓」混进已实现统计。
    """
    closed = [c for c in cycles if c.closed]
    open_cycles = list(open_cycles) or [c for c in cycles if not c.closed]

    nets = [c.net for c in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]

    gross = sum(c.gross for c in closed)
    commission = sum(c.commission for c in closed)
    maker_fee = sum(c.maker_fee for c in closed)
    taker_fee = sum(c.taker_fee for c in closed)
    funding = sum(c.funding for c in closed) + funding_orphan
    open_notional = sum(c.open_notional for c in closed)
    net = gross - commission + funding

    gross_win = sum(wins)
    gross_loss = -sum(losses)
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None

    # 已实现权益曲线按平仓时刻推进：这才是「按标的分组的最大回撤」。
    curve = sorted(((c.close_ms, c.net) for c in closed))
    cum = peak = mdd = 0.0
    for _, value in curve:
        cum += value
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    by_source: Dict[str, Any] = {}
    for name in ("strategy", "mixed", "unattributed"):
        group = [c for c in closed if c.source == name]
        if not group:
            continue
        gnet = sum(c.net for c in group)
        gw = sum(1 for c in group if c.net > 0)
        by_source[name] = {
            "trades": len(group),
            "net_pnl": gnet,
            "win_rate": gw / len(group),
            "open_notional": sum(c.open_notional for c in group),
        }

    return {
        "trade_count": len(closed),
        "closed_trades": len(closed),
        "realized_pnl": gross,
        "commission": commission,
        "maker_fee": maker_fee,
        "taker_fee": taker_fee,
        "funding_fee": funding,
        "net_pnl": net,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / avg_loss) if (avg_win and avg_loss) else None,
        "open_notional": open_notional,
        "unit_edge_bps": (net / open_notional * 10000.0) if open_notional else None,
        "realized_max_drawdown": mdd,
        "realized_net_cum": cum,
        "funding_orphan": funding_orphan,
        "open_cycles": len(open_cycles),
        "open_qty": sum(abs(c.open_qty) for c in open_cycles),
        "by_source": by_source,
    }


def coverage(cycles: Sequence[Cycle], fills: Sequence[Dict[str, Any]],
             *, truncated: bool = False, start_ms: int = 0,
             context_start_ms: int = 0) -> Dict[str, Any]:
    """覆盖度：报表必须能看出数据从哪里开始、有没有被截断。"""
    times = [int(_num(f.get("time"))) for f in fills if f.get("time")]
    first = min(times) if times else 0
    last = max(times) if times else 0
    unclosed = [c for c in cycles if not c.closed]
    return {
        "fills": len(fills),
        "first_fill_ms": first,
        "last_fill_ms": last,
        "requested_start_ms": int(start_ms or 0),
        # 实际取数起点：比记录起点更早，用来接上跨起点的持仓。
        "context_start_ms": int(context_start_ms or 0),
        # 有平仓盈亏却被配成「开仓」的成交腿数量：说明窗口起点之前已经
        # 有持仓，那些持仓的开仓腿不在数据里，周期无法完整配对。
        "orphan_closes": find_orphan_closes(fills),
        "clipped_at_start": find_orphan_closes(fills) > 0,
        "truncated": bool(truncated),
        "open_cycles": len(unclosed),
    }


def find_orphan_closes(fills: Sequence[Dict[str, Any]],
                       *, tol: float = QTY_TOL) -> int:
    """统计「带平仓盈亏、却只能配成开仓」的成交腿数。

    :func:`split_legs` 在持仓为零时只会产生 ``open`` 腿，所以一笔成交若带着
    非零 ``realizedPnl`` 却被整体判为开仓，就证明它平掉的是窗口之外的仓位——
    即数据起点把一段持仓切成了两半。这个数字必须出现在报表上，否则「按完整
    持仓周期统计」会在窗口边界处悄悄失真。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for fill in fills:
        if _signed_delta(fill) == 0:
            continue
        groups.setdefault(_position_side_key(fill), []).append(fill)

    orphan = 0
    for key in sorted(groups):
        ordered = sorted(
            groups[key],
            key=lambda f: (int(_num(f.get("time"))), int(_num(f.get("id")))),
        )
        pos = 0.0
        for fill in ordered:
            legs, _ = split_legs(pos, _signed_delta(fill), tol)
            realized = _num(fill.get("realizedPnl"))
            if abs(realized) > 1e-9 and legs and all(a == "open" for a, _ in legs):
                orphan += 1
            for _action, qty in legs:
                pos += qty
    return orphan


async def fetch_all_fills(client, symbol: str, start_ms: int = 0, *,
                          page_limit: int = PAGE_LIMIT,
                          max_pages: int = MAX_PAGES,
                          window_ms: int = WINDOW_MS) -> Tuple[List[Dict[str, Any]], bool]:
    """拉取全部成交：按 ≤7 天分窗 + 窗口内翻页，避免开仓腿被截断在窗口之外。

    两件事必须同时做对，否则会静默丢数据：

    1. **跨度限制**：币安要求 ``startTime``/``endTime`` 跨度 ≤ 7 天，超出直接
       报错；只给 ``startTime`` 不给 ``endTime`` 时跨度等于「起点到现在」，只要
       超过 7 天就永远返回空 —— 而客户端又把错误吞成空列表，于是「查不到」被
       伪装成「没有成交」。这里显式给 ``endTime`` 并按 ``window_ms`` 分窗。
    2. **窗口内翻页**：一个窗口内超过 ``page_limit`` 笔时，以上一页最后一笔
       时间 +1ms 继续取，直到取不满一页。

    ``strict=True`` 让接口错误显式抛出，不再被当成空结果。

    返回 ``(成交列表, 是否可能仍被截断)``。
    """
    now = int(time.time() * 1000)
    if not start_ms:
        # 没有起点：币安默认就是「最近 limit 笔」，一次请求即可，不必分窗。
        part = await client.user_trades(
            symbol=symbol, limit=page_limit, start_time=None,
            end_time=now, strict=True,
        )
        return _dedupe_fills(part or [], symbol), False

    cursor = int(start_ms or 0)
    rows: List[Dict[str, Any]] = []
    truncated = False

    for _ in range(max_pages):
        if cursor > now:
            break
        end = min(cursor + window_ms, now) if cursor else now
        part = await client.user_trades(
            symbol=symbol, limit=page_limit,
            start_time=cursor or None, end_time=end, strict=True,
        )
        if part:
            rows.extend(part)
            last = max((int(_num(r.get("time"))) for r in part), default=0)
            if len(part) >= page_limit and last > cursor:
                cursor = last + 1        # 同一窗口内还有更多成交
                continue
        cursor = end + 1                 # 该窗口取完，进入下一窗口
    else:
        truncated = True

    return _dedupe_fills(rows, symbol), truncated


def _dedupe_fills(rows: Iterable[Dict[str, Any]],
                  symbol: str) -> List[Dict[str, Any]]:
    """跨窗口可能有重叠，按 (symbol, id) 去重后按时间排序。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    ordered = sorted(
        rows,
        key=lambda r: (int(_num(r.get("time"))), int(_num(r.get("id")))),
    )
    for row in ordered:
        key = (str(row.get("symbol") or symbol), str(row.get("id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


async def fetch_all_income(client, symbol: str, start_ms: int = 0, *,
                           page_limit: int = 1000,
                           max_pages: int = MAX_PAGES,
                           window_ms: int = WINDOW_MS) -> List[Dict[str, Any]]:
    """资金费同样按 ≤7 天分窗拉取（与 userTrades 同一条硬性限制）。"""
    now = int(time.time() * 1000)
    cursor = int(start_ms or 0)
    rows: List[Dict[str, Any]] = []
    for _ in range(max_pages):
        if cursor and cursor > now:
            break
        end = min(cursor + window_ms, now) if cursor else now
        part = await client.income(
            symbol=symbol, income_type="FUNDING_FEE", limit=page_limit,
            start_time=cursor or None, end_time=end, strict=True,
        )
        if part:
            rows.extend(part)
            last = max((int(_num(r.get("time"))) for r in part), default=0)
            if len(part) >= page_limit and last > cursor:
                cursor = last + 1
                continue
        cursor = end + 1
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(_num(r.get("time")))):
        key = (str(row.get("tranId") or ""), str(row.get("time") or ""),
               str(row.get("income") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def tag_source(cycles: Sequence[Cycle],
               strategy_orders: Optional[Iterable[str]] = None) -> None:
    """把周期归到策略单或未识别单。

    判定只看「开仓/加仓用到的委托是否都在运行器台账里」：全部命中记
    ``strategy``，部分命中记 ``mixed``，一个都没命中记 ``unattributed``。
    台账缺失时一律 ``unattributed`` —— 按 order_sources 的既有原则，调用方
    不得把订单猜成策略单。
    """
    known = {str(x) for x in (strategy_orders or ()) if str(x)}
    for c in cycles:
        ids = {str(i) for i in (c.open_order_ids or ()) if str(i)}
        if not ids or not known:
            c.source = "unattributed"
        elif ids <= known:
            c.source = "strategy"
        elif ids & known:
            c.source = "mixed"
        else:
            c.source = "unattributed"


async def build_ledger(client, symbols: Sequence[str], start_ms: int = 0, *,
                       context_days: int = 30,
                       page_limit: int = PAGE_LIMIT,
                       max_pages: int = MAX_PAGES,
                       strategy_orders: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """取成交 + 资金费，配对成完整周期并汇总。任一步失败都不静默吞掉。

    **回看上下文**（关键）：只从记录起点取数会把「起点之前开仓、起点之后
    平仓」的持仓切成两半——平仓腿带着 realizedPnl 却没有开仓腿，手续费只算
    到一半，看起来像一笔凭空出现的盈利。因此实际取数起点提前
    ``context_days`` 天，配对完成后**只统计平仓时刻在记录起点之后的周期**，
    这样跨起点的持仓能拿到完整的手续费。
    """
    fetch_from = max(0, int(start_ms) - context_days * 86_400_000) if start_ms else 0
    per_symbol: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    all_cycles: List[Cycle] = []
    all_open: List[Cycle] = []
    all_fills: List[Dict[str, Any]] = []
    any_truncated = False
    orphan_total = 0

    for symbol in symbols:
        try:
            fills, truncated = await fetch_all_fills(
                client, symbol, fetch_from,
                page_limit=page_limit, max_pages=max_pages,
            )
        except Exception as exc:  # noqa: BLE001
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        any_truncated = any_truncated or truncated
        all_fills.extend(fills)

        income_rows: List[Dict[str, Any]] = []
        funding_note = "ok"
        try:
            income_rows = await fetch_all_income(client, symbol, fetch_from)
        except Exception as exc:  # noqa: BLE001
            funding_note = f"unavailable: {type(exc).__name__}: {exc}"
            logger.warning("资金费 %s: %s", symbol, exc)

        cycles, open_cycles = pair_cycles(fills, symbol=symbol)
        tag_source(cycles, strategy_orders)
        attr = attribute_funding(cycles, income_rows)

        in_scope = [
            c for c in cycles
            if c.closed and (not start_ms or c.close_ms >= int(start_ms))
        ]
        context_only = len(cycles) - len(in_scope) - len(open_cycles)
        orphan = find_orphan_closes(fills)
        orphan_total += orphan

        summary = summarize(in_scope, funding_orphan=attr["orphan"],
                            open_cycles=open_cycles)
        summary["coverage"] = coverage(
            in_scope, fills, truncated=truncated, start_ms=start_ms,
            context_start_ms=fetch_from,
        )
        summary["context_cycles"] = context_only
        summary["funding_note"] = funding_note
        summary["funding_rows"] = len(income_rows)
        per_symbol[symbol] = summary
        all_cycles.extend(in_scope)
        all_open.extend(open_cycles)

    total = summarize(all_cycles, open_cycles=all_open)
    total["coverage"] = coverage(
        all_cycles, all_fills, truncated=any_truncated, start_ms=start_ms,
        context_start_ms=max(0, int(start_ms) - context_days * 86_400_000) if start_ms else 0,
    )
    total["coverage"]["orphan_closes"] = orphan_total
    total["coverage"]["clipped_at_start"] = orphan_total > 0
    return {
        "per_symbol": per_symbol,
        "total": total,
        "errors": errors,
        "truncated": any_truncated,
        "context_days": context_days,
    }
