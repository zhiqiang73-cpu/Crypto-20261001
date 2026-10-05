"""交易所侧保护单（Algo 条件单）的生命周期管理。

设计原则
--------
1. **交易所是事实来源。** 「已受保护」的唯一判据是 openAlgoOrders 里能查到
   一张有效的、触发价不劣于目标的保护单。本地订单对象、HTTP 200、下单回执
   都不算验收。
2. **幂等。** 重复调用不得重复下单。先查再下，查到已覆盖就直接返回。
3. **只收紧，不放宽。** 新止损必须比旧止损更靠近价格；放宽一律拒绝。
4. **先立后破。** 更新止损时先下新单并确认，确认后才撤旧单。绝不先撤旧的。
5. **UNKNOWN 不等于失败。** 网络超时后先按 clientAlgoId 查交易所，不盲目重下，
   也不先撤旧单。

接口（币安 USDⓈ-M）
------------------
    下单   POST   /fapi/v1/algoOrder
    查询   GET    /fapi/v1/openAlgoOrders      ← 与 openOrders **不是**同一个查询
    撤单   DELETE /fapi/v1/algoOrder

    client.py 已封装为 place_stop_market / get_open_algo_orders / cancel_algo_order。
    普通 openOrders 查不到 Algo 条件单，清理保护单必须走 Algo 接口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from trading.models import OrderState

logger = logging.getLogger(__name__)

# 判定保护单是否仍然有效的状态集合。交易所侧已触发/已撤销的单不再是保护。
_ALIVE_STATUS = {"NEW", "WORKING", "PENDING", "ACCEPTED", "UNKNOWN"}

# clientAlgoId 的毫秒内序号（见 new_client_algo_id）
_ALGO_ID_SEQ = 0


class ProtectionState(str, Enum):
    PROTECTED = "PROTECTED"        # 已在交易所确认有效
    UNPROTECTED = "UNPROTECTED"    # 明确没有有效保护（下单被拒/已撤销）
    UNKNOWN = "UNKNOWN"            # 无法确认（网络/超时）—— 按未保护处理


@dataclass
class ProtectiveOrder:
    """一张交易所保护单的最终状态。"""
    symbol: str
    side: int                                   # +1 多仓，-1 空仓
    trigger_price: float = 0.0
    client_algo_id: str = ""
    algo_id: str = ""
    quantity: float = 0.0
    close_position: bool = True
    state: ProtectionState = ProtectionState.UNKNOWN
    error: str = ""
    verified: bool = False                      # 是否经 openAlgoOrders 确认
    superseded_algo_id: str = ""                # 本次更新中被撤掉的旧单
    old_cancel_ok: Optional[bool] = None        # 旧单撤销结果；None = 无需撤
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def protects(self) -> bool:
        """唯一可信的「已受保护」判据。"""
        return self.verified and self.state == ProtectionState.PROTECTED

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "方向": "多" if self.side > 0 else "空",
            "触发价": self.trigger_price,
            "clientAlgoId": self.client_algo_id,
            "algoId": self.algo_id,
            "数量": self.quantity,
            "closePosition": self.close_position,
            "状态": self.state.value,
            "已交易所确认": self.verified,
            "错误": self.error,
            "被撤旧单": self.superseded_algo_id,
            "旧单已撤": self.old_cancel_ok,
        }


# ---------------------------------------------------------------------------
# openAlgoOrders 响应归一化
# ---------------------------------------------------------------------------
def normalize_algo_orders(raw: Any) -> List[Dict[str, Any]]:
    """把 openAlgoOrders 的返回统一成 list[dict]。

    币安该接口在不同版本下可能返回裸数组，也可能包一层 {"orders": [...]}。
    两种都认，避免「查不到保护单」被误判成「没有保护」。
    """
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    if isinstance(raw, dict):
        for key in ("orders", "data", "algoOrders", "list"):
            v = raw.get(key)
            if isinstance(v, list):
                return [o for o in v if isinstance(o, dict)]
        # 单笔查询：/fapi/v1/algoOrder 可能直接返回一个对象
        if raw.get("algoId") or raw.get("clientAlgoId"):
            return [raw]
    return []


# ---------------------------------------------------------------------------
# 普通挂单（限价止盈）与 Algo 条件单的统一视图
# ---------------------------------------------------------------------------
# 2026-10-04：止盈腿从 TAKE_PROFIT_MARKET 改为 LIMIT + reduceOnly，以吃 maker
# 费率（2bp）而不是 taker（4bp），并避免市价成交的滑点。代价是止盈单从
# openAlgoOrders 搬到了 openOrders —— 两套接口的字段名、状态字、撤销端点都
# 不一样。
#
# 处理办法：**把普通挂单归一化成条件单的形状**，再与条件单合并成一份列表。
# 这样下游（find_take_profit / 去重 / 清理 / 成交对账）一行都不用改。
#
# 命名空间：普通挂单的 algoId 加前缀 "ord-"。币安的 algoId 是纯数字，
# 所以永远不会撞号 —— 合并列表里的 ID 仍然唯一。
REGULAR_ORDER_PREFIX = "ord-"


def is_regular_order(o: Dict[str, Any]) -> bool:
    """是不是被归一化过的普通挂单（而非 Algo 条件单）。"""
    if str(o.get("_source") or "") == "order":
        return True
    return algo_ident(o).startswith(REGULAR_ORDER_PREFIX)


def regular_order_id(o: Dict[str, Any]) -> str:
    """取回交易所原始 orderId（去掉命名空间前缀）。"""
    aid = algo_ident(o)
    if aid.startswith(REGULAR_ORDER_PREFIX):
        return aid[len(REGULAR_ORDER_PREFIX):]
    return str(o.get("orderId") or "")


def normalize_regular_order(o: Dict[str, Any]) -> Dict[str, Any]:
    """把 openOrders 的一条普通挂单，映射成条件单的形状。

    关键映射：
        orderId      → algoId（加 ord- 前缀）
        price        → triggerPrice（挂单价即「到达该价就成交」的触发语义，
                                     find_take_profit 因此无需改动）
        type         → orderType（"LIMIT"）
    并保留 _source="order" 与 _raw 供撤销时区分端点。
    """
    oid = str(o.get("orderId") or "")
    return {
        "algoId": f"{REGULAR_ORDER_PREFIX}{oid}" if oid else "",
        "clientAlgoId": str(o.get("clientOrderId") or ""),
        "orderType": str(o.get("type") or "LIMIT").upper(),
        "side": str(o.get("side") or "").upper(),
        "triggerPrice": _num(o.get("price") or o.get("stopPrice")),
        "quantity": _num(o.get("origQty") or o.get("quantity")),
        "status": str(o.get("status") or "NEW").upper(),
        "reduceOnly": o.get("reduceOnly"),
        # 普通限价单永远是**部分单**：币安不允许普通单带 closePosition。
        # 显式给出这个字段，让 covers_full_position / 去重逻辑拿到确定答案，
        # 而不是因为字段缺失而走默认分支。
        "closePosition": "false",
        "_source": "order",
        "_raw": o,
    }


async def fetch_open_regular_take_profits(
    client: Any, symbol: str,
) -> List[Dict[str, Any]]:
    """取该标的盘口上的普通挂单中，属于**止盈**的那些。

    只认 reduceOnly 的 LIMIT 单 —— 入场追价单也是普通挂单，混进来会被当成
    止盈单，进而被去重/清理逻辑误撤。
    """
    try:
        raw = await client.get_open_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询普通挂单失败 %s: %s", symbol, exc)
        return []
    out: List[Dict[str, Any]] = []
    for o in (raw or []):
        if not isinstance(o, dict):
            continue
        if str(o.get("type") or "").upper() != "LIMIT":
            continue
        if str(o.get("reduceOnly")).lower() not in ("true", "1"):
            continue
        n = normalize_regular_order(o)
        if n.get("algoId"):
            out.append(n)
    return out


async def fetch_open_protective_orders(
    client: Any, symbol: str,
) -> List[Dict[str, Any]]:
    """保护单的**统一视图**：Algo 条件单（止损）+ 普通挂单（限价止盈）。

    止损仍在 openAlgoOrders，止盈已在 openOrders —— 只看一边必然漏。
    漏了止盈会导致重复重挂；漏了止损会导致保护状态误判。
    """
    algo = await fetch_open_algo_orders(client, symbol)
    regular = await fetch_open_regular_take_profits(client, symbol)
    return list(algo) + list(regular)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def algo_trigger(o: Dict[str, Any]) -> float:
    for k in ("triggerPrice", "stopPrice", "activatePrice"):
        v = _num(o.get(k))
        if v > 0:
            return v
    return 0.0


def algo_side(o: Dict[str, Any]) -> str:
    return str(o.get("side") or "").upper()


def algo_ident(o: Dict[str, Any]) -> str:
    return str(o.get("algoId") or o.get("orderId") or "")


def algo_client_ident(o: Dict[str, Any]) -> str:
    return str(o.get("clientAlgoId") or o.get("clientOrderId") or "")


def algo_status(o: Dict[str, Any]) -> str:
    return str(o.get("algoStatus") or o.get("status") or "NEW").upper()


def algo_alive(o: Dict[str, Any]) -> bool:
    """该条件单是否仍然可能触发。"""
    if o.get("closePosition") is True:
        pass  # closePosition 单同样按状态判断
    st = algo_status(o)
    if st in ("CANCELED", "CANCELLED", "EXPIRED", "FINISHED", "REJECTED"):
        return False
    return st in _ALIVE_STATUS or st not in (
        "CANCELED", "CANCELLED", "EXPIRED", "FINISHED", "REJECTED")


def exit_side_of(position_side: int) -> str:
    """转成 `client.place_stop_market()` 期望的**持仓方向**参数。

    注意语义层级：client.place_stop_market(side, ...) 里
        order_side = "SELL" if side.upper() == "LONG" else "BUY"
    也就是说它吃的是「持仓方向」，自己换算成交易所的买卖方向。所以这里返回
    "LONG"/"SHORT"，**不是** "SELL"/"BUY"。

    而交易所 openAlgoOrders 回给我们的 side 字段是**买卖方向**（多仓的止损
    是 SELL）。两边语义不同，find_protective_stop 用的是后者 —— 混淆会导致
    「明明有保护单却查不到」，把受保护的仓位判成未保护。
    """
    return "LONG" if position_side > 0 else "SHORT"


# ---------------------------------------------------------------------------
# 查找覆盖当前仓位的保护单
# ---------------------------------------------------------------------------
def find_protective_stop(
    orders: List[Dict[str, Any]], *, side: int,
    required_trigger: float = 0.0, at_least_as_tight: bool = True,
) -> Optional[Dict[str, Any]]:
    """在 openAlgoOrders 里找一张能保护该方向仓位的条件单。

    side: 持仓方向（+1 多 / -1 空）。多仓的保护单是 SELL 方向，反之亦然。
    required_trigger: 目标触发价。at_least_as_tight=True 时要求已存在的单
        触发价不劣于目标 —— 多仓要求 trigger >= required（更靠近价格）；
        空仓要求 trigger <= required。
    """
    want = "SELL" if side > 0 else "BUY"
    best: Optional[Dict[str, Any]] = None
    best_trig = 0.0
    for o in orders:
        # ⚠ 多仓的止损单与止盈单**都是 SELL**。不按 orderType 过滤的话，
        # 触发价更高的止盈单会被当成「更紧的止损」选中 —— 结果是止损永远
        # 建不起来，而系统显示「已保护」，仓位实际裸奔。
        if not is_stop_type(o):
            continue
        if algo_side(o) != want:
            continue
        if not algo_alive(o):
            continue
        trig = algo_trigger(o)
        if trig <= 0:
            continue
        if required_trigger > 0 and at_least_as_tight:
            if side > 0 and trig < required_trigger:
                continue
            if side < 0 and trig > required_trigger:
                continue
        # 多仓里选触发价最高的（最紧）；空仓里选最低的
        if best is None:
            best, best_trig = o, trig
        elif (side > 0 and trig > best_trig) or (side < 0 and trig < best_trig):
            best, best_trig = o, trig
    return best


def covers_full_position(order: Dict[str, Any], *, quantity: float,
                         tol: float = 1e-9) -> bool:
    """该保护单是否覆盖全部仓位。

    closePosition=true 的条件单由交易所保证覆盖全部仓位，加层后自动跟随，
    不需要重挂；显式带数量的单必须数量 >= 当前净仓才算覆盖。
    """
    if str(order.get("closePosition")).lower() == "true":
        return True
    q = _num(order.get("quantity") or order.get("origQty"))
    return q + tol >= quantity > 0


# ---------------------------------------------------------------------------
# 下单 / 确认
# ---------------------------------------------------------------------------
def algo_order_type(o: Dict[str, Any]) -> str:
    """条件单类型：STOP_MARKET / TAKE_PROFIT_MARKET / ..."""
    return str(o.get("orderType") or o.get("type") or "").upper()


def is_stop_type(o: Dict[str, Any]) -> bool:
    """是不是止损类条件单。

    字段缺失时**按止损处理**：老响应与既有测试不带 orderType，收紧默认值会
    让它们全部失效。只有明确标了 TAKE_PROFIT 的才排除。
    """
    t = algo_order_type(o)
    return not t or "STOP" in t


def is_take_profit_type(o: Dict[str, Any]) -> bool:
    """是不是止盈单（Algo 条件单 或 归一化后的普通挂单）。

    字段缺失时**不**当止盈 —— 宁可漏认也不能误认。

    2026-10-04：止盈改为 LIMIT + reduceOnly 后，普通挂单的 orderType 是
    "LIMIT"，不含 TAKE_PROFIT 字样，必须显式识别，否则 find_take_profit
    永远找不到已挂的止盈单，表现为「反复重挂」。
    """
    if "TAKE_PROFIT" in algo_order_type(o):
        return True
    return is_regular_order(o)


def find_take_profit(
    orders: List[Dict[str, Any]], *, side: int, trigger: float,
    tolerance: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """找一张触发价等于 trigger 的止盈单（容差 tolerance 内）。

    只认 orderType 里带 TAKE_PROFIT 的单 —— 多仓的止损单与止盈单都是 SELL，
    不按类型过滤会把止损单当成止盈单（或反过来），后果是保护完全失效。
    """
    # ⚠ 这里要的是**交易所买卖方向**，不是持仓方向。
    # exit_side_of() 返回 "LONG"/"SHORT"（那是给 place_stop_market 的持仓方向
    # 参数），而 openAlgoOrders 回给我们的 side 字段是 "SELL"/"BUY"。用错就永远
    # 匹配不上，表现是「明明挂着止盈单却反复重挂」。
    want = "SELL" if side > 0 else "BUY"
    for o in orders:
        if not is_take_profit_type(o) or not algo_alive(o):
            continue
        if algo_side(o) != want:
            continue
        t = algo_trigger(o)
        if t <= 0:
            continue
        if abs(t - trigger) <= max(tolerance, 0.0):
            return o
    return None


async def place_take_profit(
    client: Any, *, symbol: str, side: int, trigger_price: float,
    quantity: float, tag: str = "tp",
) -> ProtectiveOrder:
    """挂一张部分仓位的**限价**止盈单（LIMIT + reduceOnly + quantity）。

    与止损单的关键差别：止损用 closePosition=true 覆盖全仓且必须市价；止盈
    必须指定数量，且**不能**同时传 closePosition（两者互斥）。

    2026-10-04 变更：TAKE_PROFIT_MARKET → LIMIT + reduceOnly。
      多头的止盈价在市场**上方**，一张挂在那里的 SELL LIMIT 天然是 **maker**，
      根本不需要触发机制。收益是双重的：
        * 费率：taker 4bp → maker 2bp
        * 成交价：按限价成交，不吃市价滑点
      代价：价格没到就反转则不成交 —— 但仓位仍在，追踪止损继续兜底，
      **下行有界**，所以这个代价是可以接受的。
    止损**保持市价不动**：省 2bp 去换「可能没止损掉」是把确定的尾部风险
    换成确定的小钱，这笔交易不该做。
    """
    cid = new_client_algo_id(symbol, tag)
    try:
        res = await client.place_resting_limit_order(
            exit_side_of(side), quantity, trigger_price, symbol,
            reduce_only=True, client_order_id=cid,
        )
        # 币安对限价单有**价格带限制**（实测约 ±5%，超限报 -4016）。
        # 这在实际场景里会出现：价格大幅下跌后，多头的止盈目标
        # （entry + 2×ATR）可能落到现价上方 5% 以外，限价单直接挂不上去。
        # 此时**降级为市价止盈** —— 多付 2bp 也比完全没有止盈强。
        # 只在价格带错误上降级：网络类错误不能走这条路，否则会重复下单。
        if not getattr(res, "ok", False) and "-4016" in str(
                getattr(res, "error", "") or ""):
            print(f"[{symbol} 止盈] 限价 {trigger_price:.2f} 超出交易所价格带，"
                  f"降级为市价止盈（taker）")
            # 局部导入：shadow.alerts 是零依赖叶子模块，但 trading→shadow 是
            # 分层倒置。放在函数内导入可彻底避免导入期循环，代价只是每次多一次
            # 字典查找（sys.modules 命中）。这是有意为之的取舍，不是疏漏。
            from shadow.alerts import notify
            notify("WARNING", f"tp_limit_out_of_band_{symbol}",
                   f"{symbol} 止盈价超出限价单价格带，已降级为市价止盈",
                   f"触发价 {trigger_price:.2f}；原因: {getattr(res, 'error', '')}")
            res = await client.place_stop_market(
                exit_side_of(side), quantity, trigger_price, symbol,
                client_order_id=cid, close_position=False,
                order_type="TAKE_PROFIT_MARKET",
            )
    except Exception as exc:  # noqa: BLE001
        return ProtectiveOrder(
            symbol=symbol, side=side, trigger_price=float(trigger_price),
            client_algo_id=cid, state=ProtectionState.UNKNOWN,
            error=f"{type(exc).__name__}: {exc}",
        )
    state = getattr(res, "state", None)
    ok = bool(getattr(res, "ok", False)) or str(state) in (
        "OrderState.ACKNOWLEDGED", "OrderState.FILLED", "ACKNOWLEDGED", "FILLED")
    algo_id = str(getattr(res, "algo_id", "") or "")
    if not algo_id:
        # 普通挂单的 OrderResult 用 order_id；统一加上命名空间前缀，
        # 使下游的 algo_ident / 撤销分发都能正确识别来源。
        oid = str(getattr(res, "order_id", "") or "")
        if oid:
            algo_id = f"{REGULAR_ORDER_PREFIX}{oid}"
    if not ok or not algo_id:
        # 下单超时/无回执时按 clientAlgoId 回查，不能盲目重复下单
        found = await _verify_by_client_id(client, symbol, cid)
        if found is not None:
            return ProtectiveOrder(
                symbol=symbol, side=side,
                trigger_price=algo_trigger(found),
                algo_id=algo_ident(found), client_algo_id=cid,
                verified=True, state=ProtectionState.PROTECTED,
            )
        return ProtectiveOrder(
            symbol=symbol, side=side, trigger_price=float(trigger_price),
            client_algo_id=cid, state=ProtectionState.UNKNOWN,
            error=str(getattr(res, "error", "") or "下单未确认"),
        )
    return ProtectiveOrder(
        symbol=symbol, side=side, trigger_price=float(trigger_price),
        algo_id=algo_id, client_algo_id=cid, verified=True,
        state=ProtectionState.PROTECTED,
    )


async def _verify_by_client_id(client: Any, symbol: str,
                               client_algo_id: str) -> Optional[Dict[str, Any]]:
    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception:  # noqa: BLE001
        return None
    for o in orders:
        if algo_client_ident(o) == client_algo_id:
            return o
    return None


async def cancel_algo_by_id(client: Any, symbol: str, algo_id: str) -> bool:
    """按单号撤一张保护单。返回是否确认已不在盘口。

    **必须分发端点**：带 ord- 前缀的是普通挂单（限价止盈），走
    /fapi/v1/order；其余是 Algo 条件单（止损），走 Algo 接口。
    用错端点的表现是「接口返回成功但单子还在」—— 比报错更危险，
    调用方会以为清理干净了。2026-10-04 止盈改限价后实测踩到过。
    """
    if str(algo_id).startswith(REGULAR_ORDER_PREFIX):
        oid = str(algo_id)[len(REGULAR_ORDER_PREFIX):]
        try:
            await client.cancel_order(order_id=oid, symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol}] 撤挂单 {algo_id} 失败: {exc}")
            return False
        return await _verify_absent(client, symbol, algo_id)
    try:
        await client.cancel_algo_order(algo_id=algo_id, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] 撤条件单 {algo_id} 失败: {exc}")
        return False
    return await _verify_absent(client, symbol, algo_id)


async def _verify_absent(client: Any, symbol: str, algo_id: str) -> bool:
    """确认该单号已不在盘口 —— 普通挂单与条件单查各自的接口。

    只查一边会把「普通挂单还在」误判成「已撤干净」。
    """
    try:
        if str(algo_id).startswith(REGULAR_ORDER_PREFIX):
            orders = await fetch_open_regular_take_profits(client, symbol)
        else:
            orders = await fetch_open_algo_orders(client, symbol)
    except Exception:  # noqa: BLE001
        return False
    return not any(algo_ident(o) == algo_id for o in orders)

async def fetch_open_algo_orders(client: Any, symbol: str) -> List[Dict[str, Any]]:
    """查询交易所当前有效的 Algo 条件单。异常时抛给调用方，不吞。"""
    raw = await client.get_open_algo_orders(symbol)
    return normalize_algo_orders(raw)


def new_client_algo_id(symbol: str, tag: str = "prot") -> str:
    """生成本地 clientAlgoId。

    必须由**调用方**生成并在下单前就确定：下单超时（UNKNOWN）时唯一的自救
    办法是按这个 ID 回查交易所，确认单子到底有没有落地。等下单函数自己生成
    就来不及了 —— 超时那一刻我们还不知道它用了什么 ID。
    """
    import os
    import time
    global _ALGO_ID_SEQ
    _ALGO_ID_SEQ = (_ALGO_ID_SEQ + 1) % 1000
    clean = "".join(c for c in symbol if c.isalnum())[:6].lower()
    # 序号必须带上：毫秒级时间戳在同一毫秒内会撞号，而重复的 clientAlgoId
    # 会让「按 ID 回查」查到别人，或者被交易所当重复委托拒掉。
    return (f"{tag}{clean}{int(time.time() * 1000) % 10_000_000_000}"
            f"{os.getpid() % 100:02d}{_ALGO_ID_SEQ:03d}")


async def _verify_present(client: Any, symbol: str, *,
                          client_algo_id: str, algo_id: str,
                          trigger: float, side: int,
                          required_trigger: float) -> tuple:
    """复核保护单是否真的在交易所有效。返回 (是否有效, 订单dict, 说明)。"""
    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001 查不到 ≠ 没有，保持 UNKNOWN
        return False, None, f"openAlgoOrders 查询失败: {exc}"
    for o in orders:
        if client_algo_id and algo_client_ident(o) == client_algo_id:
            return True, o, "按 clientAlgoId 命中"
        if algo_id and algo_ident(o) == algo_id:
            return True, o, "按 algoId 命中"
    # 没按 ID 命中，退一步看有没有别的单已经覆盖到目标水平
    alt = find_protective_stop(orders, side=side,
                               required_trigger=required_trigger)
    if alt is not None:
        return True, alt, "未命中本次单号，但已存在同等或更紧的保护单"
    return False, None, "下单后 openAlgoOrders 中查不到该保护单"


async def place_protective_stop(
    client: Any, *, symbol: str, side: int, trigger_price: float,
    quantity: float = 0.0, close_position: bool = True,
    client_algo_id: Optional[str] = None,
    required_trigger: Optional[float] = None,
) -> ProtectiveOrder:
    """下一张保护单并**向交易所确认**它真的有效。

    只有 openAlgoOrders 里查到，才返回 PROTECTED；否则 UNKNOWN/UNPROTECTED。
    调用方必须用 .protects 判断，不能看 HTTP 是否成功。
    """
    out = ProtectiveOrder(symbol=symbol, side=side,
                          trigger_price=trigger_price, quantity=quantity,
                          close_position=close_position)
    if trigger_price <= 0:
        out.state = ProtectionState.UNPROTECTED
        out.error = "触发价非法"
        return out
    req = required_trigger if required_trigger is not None else trigger_price
    kwargs: Dict[str, Any] = {}
    # 下单前就把 clientAlgoId 定下来。超时那一刻如果还不知道它用了什么 ID，
    # 就没法回查交易所，只能盲目重下 —— 那正是重复保护单的来源。
    cid = client_algo_id or new_client_algo_id(symbol)
    out.client_algo_id = cid
    kwargs["client_order_id"] = cid
    try:
        mo = await client.place_stop_market(
            exit_side_of(side), quantity, trigger_price, symbol,
            close_position=close_position, **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        # 超时不等于失败：交易所可能已经接单。先按 clientAlgoId 回查再下结论，
        # 不盲目重下（会变成两张保护单，触发时重复平仓）。
        ok, found, why = await _verify_present(
            client, symbol, client_algo_id=cid, algo_id="",
            trigger=trigger_price, side=side, required_trigger=req,
        )
        if ok and found is not None:
            out.verified = True
            out.state = ProtectionState.PROTECTED
            out.algo_id = algo_ident(found)
            out.raw = dict(found)
            return out
        out.state = ProtectionState.UNKNOWN
        out.error = f"下单异常({exc})；回查未确认: {why}"
        return out

    out.client_algo_id = str(getattr(mo, "client_order_id", "") or cid)
    out.algo_id = str(getattr(mo, "algo_id", "") or "")
    out.raw = dict(getattr(mo, "raw", {}) or {})
    state = getattr(mo, "state", None)
    # ⚠ OrderState 的枚举值是**小写**（'rejected' / 'canceled'），
    # 拿 "REJECTED" 这种大写字面量比较会永远不成立 —— 被拒的下单会被
    # 误判成 UNKNOWN，进而触发「回查 → 查不到 → 按未保护处理」的错误链路。
    if state == OrderState.REJECTED:
        out.state = ProtectionState.UNPROTECTED
        out.error = str(getattr(mo, "error", "") or "下单被拒")
        return out

    ok, found, why = await _verify_present(
        client, symbol, client_algo_id=out.client_algo_id,
        algo_id=out.algo_id, trigger=trigger_price, side=side,
        required_trigger=req,
    )
    if ok and found is not None:
        out.verified = True
        out.state = ProtectionState.PROTECTED
        out.algo_id = out.algo_id or algo_ident(found)
        out.client_algo_id = out.client_algo_id or algo_client_ident(found)
        out.error = ""
        return out
    # 下单可能已被接受，只是查询这一下没看到 —— 保持 UNKNOWN，绝不谎报已保护。
    # ⚠ 但**交易所返回的原始错误必须带出来**：2026-10-05 实测，下单被 -4130
    # 明确拒绝时，这里只留下「查不到」的通用文案，真实原因整个丢掉 ——
    # 日志里 -4130 出现 0 次，诊断方向被误导了一整天。
    mo_error = str(getattr(mo, "error", "") or "")
    out.state = ProtectionState.UNKNOWN
    out.error = (f"{why}；交易所返回: {mo_error}" if mo_error else why)
    return out


async def tighten_protective_stop(
    client: Any, *, symbol: str, side: int, new_trigger: float,
    old_algo_id: str = "", old_client_algo_id: str = "",
    quantity: float = 0.0, close_position: bool = True,
    client_algo_id: Optional[str] = None,
) -> ProtectiveOrder:
    """把保护单收紧到 new_trigger —— **先立后破**。

    流程：查现状 → 需要则下新单 → 确认新单在交易所有效 → 才撤旧单 → 复核。
    任何一步无法确认，都保持旧单不动，返回 UNKNOWN，由调用方按未确认处理。

    旧单撤销失败时返回的 ProtectiveOrder 里 old_cancel_ok=False，
    但 state 仍为 PROTECTED —— 因为**新保护已经生效**，风险是双向挂单可能
    在触发时重复平仓，需要调用方报警重试，而不是当作未受保护。
    """
    out = ProtectiveOrder(symbol=symbol, side=side, trigger_price=new_trigger,
                          quantity=quantity, close_position=close_position)
    if new_trigger <= 0:
        out.state = ProtectionState.UNPROTECTED
        out.error = "新触发价非法"
        return out

    # 1) 现状：是不是已经有一张同等或更紧的单了（幂等）
    try:
        orders = await fetch_open_algo_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        out.state = ProtectionState.UNKNOWN
        out.error = f"收紧前查询失败: {exc}"
        return out
    existing = find_protective_stop(orders, side=side,
                                    required_trigger=new_trigger)
    if existing is not None:
        trig = algo_trigger(existing)
        out.verified = True
        out.state = ProtectionState.PROTECTED
        out.trigger_price = trig
        out.algo_id = algo_ident(existing)
        out.client_algo_id = algo_client_ident(existing)
        out.error = ""
        # 目标水平已被满足，无需再下新单；旧单如与它是同一张则无需撤
        if old_algo_id and out.algo_id == old_algo_id:
            out.old_cancel_ok = None
        return out

    # 2) 先立：下新单并确认
    placed = await place_protective_stop(
        client, symbol=symbol, side=side, trigger_price=new_trigger,
        quantity=quantity, close_position=close_position,
        client_algo_id=client_algo_id,
    )
    if not placed.protects:
        # 新单没确认 —— 绝不能撤旧单，旧保护必须留着
        placed.error = (f"新保护单未确认，保留旧保护单不动；{placed.error}")
        placed.superseded_algo_id = ""
        placed.old_cancel_ok = None
        return placed

    out.verified = True
    out.state = ProtectionState.PROTECTED
    out.algo_id = placed.algo_id
    out.client_algo_id = placed.client_algo_id
    out.raw = placed.raw

    # 3) 后破：新单确认后才撤旧单
    old_id = old_algo_id or old_client_algo_id
    if not old_id or out.algo_id == old_algo_id:
        out.old_cancel_ok = None
        return out
    out.superseded_algo_id = old_id
    try:
        if old_algo_id:
            res = await client.cancel_algo_order(algo_id=old_algo_id,
                                                 symbol=symbol)
        else:
            res = await client.cancel_algo_order(
                client_algo_id=old_client_algo_id, symbol=symbol)
        st = getattr(res, "state", None)
        out.old_cancel_ok = (st == OrderState.CANCELED)
        if not out.old_cancel_ok:
            out.error = (f"旧保护单撤销未确认: "
                         f"{getattr(res, 'error', '') or getattr(st, 'value', st)}")
    except Exception as exc:  # noqa: BLE001 撤旧失败不影响新保护已生效的事实
        out.old_cancel_ok = False
        out.error = f"旧保护单撤销异常: {exc}"
    return out


async def cancel_protective_orders(
    client: Any, symbol: str, *, include_take_profit: bool = False,
) -> tuple:
    """撤销该标的的保护单（Algo 条件单 + 普通挂单止盈）。返回 (撤销数, 剩余数)。

    **两套接口都要查、都要撤**：止损在 openAlgoOrders（走 Algo 接口撤），
    限价止盈在 openOrders（走 /fapi/v1/order 撤）。只查一边的后果是「以为撤
    干净了」而实际把单子留在交易所，影响下一笔仓位 —— 撤销时用错端点更危险，
    因为接口会返回成功。

    include_take_profit: **默认 False，只撤止损单。**
        这里曾经是无差别撤销全部 Algo 单，把止盈单一起撤了。后果不只是丢了
        止盈：止盈单消失后，reconcile_tp_fills 会把「单子没了」当成「已成交」，
        于是批次被跳过（实测 BTC 直接跳过了 2×ATR 的批次）。清理重复止损单
        绝不该碰止盈单。**只有仓位归零时才传 True** —— 那时才需要把残留的
        止盈单也一并清掉，否则下一笔仓位会挂着一张旧止盈单。
    """
    try:
        orders = await fetch_open_protective_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"撤销保护单前查询失败: {exc}") from exc
    n = 0
    for o in orders:
        if not algo_alive(o):
            continue
        if not include_take_profit and not is_stop_type(o):
            continue
        aid = algo_ident(o)
        cid = algo_client_ident(o)
        if not aid and not cid:
            continue
        try:
            # 分发到正确的端点：普通挂单走 /fapi/v1/order，条件单走 Algo 接口。
            # 用错端点的表现是「撤销返回成功但单子还在」—— 比报错更危险。
            if is_regular_order(o):
                oid = regular_order_id(o)
                if oid:
                    await client.cancel_order(order_id=oid, symbol=symbol)
                else:
                    await client.cancel_order(
                        client_order_id=cid, symbol=symbol)
            elif aid:
                await client.cancel_algo_order(algo_id=aid, symbol=symbol)
            else:
                await client.cancel_algo_order(client_algo_id=cid, symbol=symbol)
            n += 1
        except Exception as exc:  # noqa: BLE001 单张失败不阻断其余
            logger.warning("撤销保护单失败 %s %s: %s", symbol, aid or cid, exc)
    try:
        left = await fetch_open_protective_orders(client, symbol)
    except Exception:  # noqa: BLE001
        left = []
    remaining = sum(1 for o in left if algo_alive(o))
    return n, remaining


# ---------------------------------------------------------------------------
# 启动对账
# ---------------------------------------------------------------------------
@dataclass
class ReconcileResult:
    """启动/重启时对账的结果。"""
    symbol: str
    side: int = 0
    quantity: float = 0.0
    state: ProtectionState = ProtectionState.UNKNOWN
    action: str = ""                       # 本次做了什么
    stop: Optional[ProtectiveOrder] = None
    duplicates_canceled: int = 0
    note: str = ""

    @property
    def can_trade(self) -> bool:
        """能否恢复开仓。只有确认受保护、或确实空仓，才允许。"""
        if abs(self.quantity) <= 0:
            return True
        return self.state == ProtectionState.PROTECTED

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "方向": "多" if self.side > 0 else ("空" if self.side < 0 else "空仓"),
            "数量": self.quantity,
            "状态": self.state.value,
            "本次动作": self.action,
            "可恢复开仓": self.can_trade,
            "重复保护单已撤": self.duplicates_canceled,
            "说明": self.note,
            "保护单": self.stop.as_dict() if self.stop else None,
        }


async def reconcile_protective(
    client: Any, *, symbol: str, side: int, quantity: float,
    required_trigger: float,
) -> ReconcileResult:
    """进程启动/重启时的保护单对账。

    以交易所实仓 + openAlgoOrders 为唯一事实来源：
      * 空仓 → 清理残留保护单（上一笔的不能留到下一笔）
      * 有仓且已有不劣于目标的保护单 → PROTECTED，撤掉多余重复单
      * 有仓但没有合格保护单 → UNPROTECTED，**不允许恢复开仓**
      * 查询失败 → UNKNOWN，同样不允许恢复开仓
    """
    res = ReconcileResult(symbol=symbol, side=side, quantity=quantity)
    try:
        orders = await fetch_open_protective_orders(client, symbol)
    except Exception as exc:  # noqa: BLE001
        res.state = ProtectionState.UNKNOWN
        res.note = f"openAlgoOrders 查询失败，按未保护处理: {exc}"
        return res

    if abs(quantity) <= 0:
        if orders:
            n, left = await cancel_protective_orders(
                client, symbol, include_take_profit=True)
            res.action = f"空仓，清理残留保护单 {n} 张"
            res.duplicates_canceled = n
            res.note = f"剩余未撤 {left} 张" if left else "已清理干净"
        else:
            res.action = "空仓且无残留保护单"
        res.state = ProtectionState.PROTECTED if not orders else ProtectionState.UNKNOWN
        return res

    good = find_protective_stop(orders, side=side,
                                required_trigger=required_trigger)
    if good is None:
        res.state = ProtectionState.UNPROTECTED
        res.action = "有仓但无合格保护单"
        res.note = (f"需要触发价{'≥' if side > 0 else '≤'}"
                    f"{required_trigger} 的保护单；交易所现有 {len(orders)} 张")
        return res

    stop = ProtectiveOrder(
        symbol=symbol, side=side, trigger_price=algo_trigger(good),
        algo_id=algo_ident(good), client_algo_id=algo_client_ident(good),
        quantity=quantity, verified=True, state=ProtectionState.PROTECTED,
    )
    res.stop = stop
    res.state = ProtectionState.PROTECTED
    res.action = "已有合格保护单，无需重挂"

    # 同一方向有多张存活保护单 → 触发时会重复平仓，撤掉多余的
    want = "SELL" if side > 0 else "BUY"
    dupes = [o for o in orders
             if algo_side(o) == want and algo_alive(o)
             and algo_ident(o) != stop.algo_id]
    if dupes:
        n = 0
        for o in dupes:
            aid = algo_ident(o)
            try:
                if aid:
                    await client.cancel_algo_order(algo_id=aid, symbol=symbol)
                else:
                    await client.cancel_algo_order(
                        client_algo_id=algo_client_ident(o), symbol=symbol)
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理重复保护单失败 %s %s: %s", symbol, aid, exc)
        res.duplicates_canceled = n
        res.action = f"保留最紧的一张，清理重复保护单 {n} 张"
    return res
