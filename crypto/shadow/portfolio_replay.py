"""离线组合回放：没有网络、交易客户端、文件写入或运行器入口。

输入已收盘且通过 MACD 方向过滤的信号、离散 mark/ATR 轮询、成交回报与资金费。
目标虚拟账本与实际净仓分离；只有确认成交才产生仓位/手续费/权益。
OHLC 路径仅供上下界情景分析，不得称为真实 mark 或真实成交。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

from shadow.strategy_books import (SPEC_15M, SPEC_5M, SPEC_ETH_15M,
                                   SPEC_ETH_5M, TRADE_SYMBOLS)

SPECS = {s.id: s for s in (SPEC_15M, SPEC_5M, SPEC_ETH_15M, SPEC_ETH_5M)}
DAY_MS = 86_400_000


def _positive(value: float, label: str) -> None:
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{label} 必须为正的有限数")


@dataclass(frozen=True)
class Signal:
    close_ms: int                      # 已收盘时间，绝不接受未来 K 线
    leg_id: str
    side: int                          # 1 / -1 / 0 (0=仅平该虚拟腿)
    qty: float                         # 请求数量，并非已确认成交
    atr_1h: float                      # 当时最后一根已收盘 1h ATR
    macd_hist: Optional[float] = None  # 提供时复核方向；None=上游已完成闸门


@dataclass(frozen=True)
class Poll:
    ms: int
    mark: Mapping[str, float]
    current_atr_1h: Mapping[str, float]
    trade_px: Mapping[str, float] = field(default_factory=dict)
    fill_fraction: Mapping[str, float] = field(default_factory=dict)
    fee_rate: Mapping[str, float] = field(default_factory=dict)
    funding_rate: Mapping[str, float] = field(default_factory=dict)
    funding_cash: Mapping[str, float] = field(default_factory=dict)
    # 来自独立历史成交记录时：按回报的 signed qty/价格/佣金入账，必须有去重 ID。
    confirmed_fill: Mapping[str, float] = field(default_factory=dict)
    fill_id: Mapping[str, str] = field(default_factory=dict)
    commission_cash: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayConfig:
    initial_equity: float = 10_000.0
    enabled_legs: Tuple[str, ...] = ("kdj15", "eth15")  # 线上快照仅两条 15m
    poll_interval_ms: int = 15_000
    signal_delay_ms: int = 0
    fee_per_side: float = 0.0005
    slippage_bps: float = 2.0
    qty_step: float = 0.001
    normal_stop_atr: Optional[float] = None  # 候选；线上未启用
    break_even_trigger_atr: Optional[float] = None  # 候选；线上未启用
    break_even_buffer_bps: float = 0.0  # 入场价之外的候选成本缓冲；非保证净保本
    disaster_atr: float = 3.0
    daily_loss_limit: float = 0.03
    daily_latch: bool = False  # 默认匹配 deploy.step 每轮重算；True=研究用锁日
    total_drawdown_limit: float = 0.10
    # 可选的研究预算；基线 None 不改变线上信号请求量。
    leg_risk_limit: Optional[float] = None
    portfolio_risk_limit: Optional[float] = None
    gross_leverage_limit: Optional[float] = None

    def __post_init__(self) -> None:
        _positive(self.initial_equity, "initial_equity")
        _positive(self.qty_step, "qty_step")
        _positive(self.disaster_atr, "disaster_atr")
        if self.poll_interval_ms <= 0 or self.signal_delay_ms < 0:
            raise ValueError("轮询间隔必须为正且信号延迟非负")
        if not self.enabled_legs or len(set(self.enabled_legs)) != len(self.enabled_legs):
            raise ValueError("策略列表不得为空或重复")
        if any(leg not in SPECS for leg in self.enabled_legs):
            raise ValueError("未知策略")
        for name in ("normal_stop_atr", "break_even_trigger_atr", "leg_risk_limit",
                     "portfolio_risk_limit", "gross_leverage_limit"):
            v = getattr(self, name)
            if v is not None:
                _positive(v, name)
        for name in ("fee_per_side", "slippage_bps", "break_even_buffer_bps"):
            v = getattr(self, name)
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} 不得为负或非有限数")
        for name in ("daily_loss_limit", "total_drawdown_limit"):
            v = getattr(self, name)
            if not math.isfinite(v) or not 0 < v < 1:
                raise ValueError(f"{name} 必须位于 (0,1)")


@dataclass
class Leg:
    side: int
    qty: float
    atr_at_signal: float
    signal_ms: int
    # 只能在同向的真实净仓成交确认后冻结；完全抵消的虚拟腿不声称有价格止损。
    entry_px: Optional[float] = None
    break_even_armed: bool = False


@dataclass
class Net:
    qty: float = 0.0
    entry_px: float = 0.0


@dataclass
class ReplayResult:
    cash: float
    equity: float
    peak: float
    positions: Dict[str, Net]
    legs: Dict[str, Optional[Leg]]
    equity_curve: list
    events: list
    fees: float
    funding: float
    slippage_cost: float
    realized_pnl: float
    halted: bool
    daily_blocked: bool
    missed_signals: int
    pending_targets: Dict[str, float]


class PortfolioReplay:
    """纯内存决策与核算；并非可连接交易所的订单路由器。"""

    def __init__(self, cfg: Optional[ReplayConfig] = None):
        self.cfg = cfg or ReplayConfig()
        self.cash = self.cfg.initial_equity
        self.peak = self.cash
        self.net = {s: Net() for s in TRADE_SYMBOLS}
        self.legs: Dict[str, Optional[Leg]] = {s: None for s in self.cfg.enabled_legs}
        self.pending = {s: 0.0 for s in TRADE_SYMBOLS}
        self.emergency = {s: False for s in TRADE_SYMBOLS}
        self.stop_hold = {s: False for s in TRADE_SYMBOLS}
        self.fees = self.funding = self.slippage_cost = self.realized = 0.0
        self.halted = self.daily_blocked = False
        self.day = None
        self.day_start = self.cash
        self.events: list = []
        self.curve: list = []
        self.missed_signals = 0
        self._seen_fills = set()
        self._used = False

    def _log(self, ts: int, kind: str, **detail) -> None:
        self.events.append({"ts": ts, "kind": kind, **detail})

    def _equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + sum(p.qty * (marks[s] - p.entry_px)
                               for s, p in self.net.items())

    def _record_fill(self, ts: int, symbol: str, qty: float, px: float,
                     fee: float, *, source: str, reference: Optional[float] = None) -> None:
        p = self.net[symbol]
        old = p.qty
        closed = min(abs(old), abs(qty)) if old * qty < 0 else 0.0
        realized = closed * (px - p.entry_px) * (1 if old > 0 else -1)
        new = old + qty
        if abs(new) < 1e-10:
            new, entry = 0.0, 0.0
        elif old * qty >= 0:
            entry = (abs(old) * p.entry_px + abs(qty) * px) / abs(new)
        elif old * new > 0:
            entry = p.entry_px
        else:
            entry = px  # 净仓翻向后余量以实际成交价作为新均价
        p.qty, p.entry_px = new, entry
        self.cash += realized - fee
        self.realized += realized
        self.fees += fee
        if reference is not None:
            self.slippage_cost += abs(qty) * abs(px - reference)
        self._log(ts, "fill", symbol=symbol, source=source, signed_qty=qty, price=px,
                  fee=fee, realized=realized, net=new, entry=entry)
        for leg_id, leg in self.legs.items():
            if leg is not None and SPECS[leg_id].symbol == symbol and leg.entry_px is None:
                if new * leg.side > 0:
                    leg.entry_px = entry
                    self._log(ts, "leg_activated", leg=leg_id, price=entry,
                              atr_at_signal=leg.atr_at_signal)

    def _validate_poll(self, poll: Poll, last_ms: Optional[int]) -> None:
        if last_ms is not None and poll.ms - last_ms < self.cfg.poll_interval_ms:
            raise ValueError("相邻轮询不足配置间隔或时间倒退")
        if set(poll.mark) != set(TRADE_SYMBOLS):
            raise ValueError("每次轮询须同时提供 BTC/ETH mark，以防账户权益漏腿")
        for sym in TRADE_SYMBOLS:
            _positive(poll.mark[sym], f"{sym} mark")
        for name in ("current_atr_1h", "trade_px", "fee_rate", "funding_rate",
                     "funding_cash", "fill_fraction", "confirmed_fill", "fill_id",
                     "commission_cash"):
            if set(getattr(poll, name)) - set(TRADE_SYMBOLS):
                raise ValueError(f"{name} 存在未知标的")
        for sym, value in poll.current_atr_1h.items():
            _positive(value, f"{sym} current ATR")
        for sym, value in poll.trade_px.items():
            _positive(value, f"{sym} trade_px")
        for sym, value in poll.fill_fraction.items():
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{sym} fill_fraction 必须位于 [0,1]")
        for name in ("fee_rate", "commission_cash"):
            for value in getattr(poll, name).values():
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{name} 必须非负且有限")
        for name in ("funding_rate", "funding_cash", "confirmed_fill"):
            for value in getattr(poll, name).values():
                if not math.isfinite(value):
                    raise ValueError(f"{name} 必须有限")
        for sym in TRADE_SYMBOLS:
            if sym in poll.funding_rate and sym in poll.funding_cash:
                raise ValueError("实际资金费与估算资金费不得重复记账")
            if sym in poll.confirmed_fill:
                if (sym in poll.fill_fraction or sym not in poll.trade_px
                        or not poll.fill_id.get(sym)):
                    raise ValueError("确认成交须有成交价/唯一 ID，且不可与模型成交同轮混用")
                if sym not in poll.commission_cash and sym not in poll.fee_rate:
                    raise ValueError("确认成交须附实际佣金或明确费率")
            elif sym in poll.commission_cash or sym in poll.fill_id:
                raise ValueError("孤立佣金或成交 ID")

    def _confirmed_fills(self, poll: Poll) -> None:
        for sym, qty in poll.confirmed_fill.items():
            key = (sym, poll.fill_id[sym])
            if key in self._seen_fills:
                self._log(poll.ms, "duplicate_fill_ignored", symbol=sym, fill_id=key[1])
                continue
            gap = self.pending[sym] - self.net[sym].qty
            if (abs(qty) < 1e-12 or qty * gap <= 0
                    or abs(qty) > abs(gap) + 1e-9):
                raise ValueError("确认成交不匹配待核目标；需人工核账，不可自动归因")
            self._seen_fills.add(key)
            px = poll.trade_px[sym]
            fee = poll.commission_cash.get(sym, abs(qty) * px * poll.fee_rate.get(sym, self.cfg.fee_per_side))
            self._record_fill(poll.ms, sym, qty, px, fee, source="confirmed")

    def _funding(self, poll: Poll) -> None:
        for sym in TRADE_SYMBOLS:
            if sym in poll.funding_cash:
                amount = poll.funding_cash[sym]  # 带符号的账户收入/支出 USDT
            elif sym in poll.funding_rate:
                amount = -self.net[sym].qty * poll.mark[sym] * poll.funding_rate[sym]
            else:
                continue
            self.cash += amount
            self.funding += amount
            self._log(poll.ms, "funding", symbol=sym, cash=amount)

    def _gates(self, poll: Poll) -> float:
        equity = self._equity(poll.mark)
        self.peak = max(self.peak, equity)
        day = poll.ms // DAY_MS  # UTC；不是本机时区
        if day != self.day:
            self.day, self.day_start = day, equity
            self.daily_blocked = False
        daily = (self.day_start - equity) / self.day_start if self.day_start > 0 else 1.0
        dd = (self.peak - equity) / self.peak
        if dd >= self.cfg.total_drawdown_limit and not self.halted:
            self.halted = True
            self._log(poll.ms, "total_drawdown_halt", equity=equity, drawdown=dd)
        if daily >= self.cfg.daily_loss_limit and not self.daily_blocked:
            self._log(poll.ms, "daily_loss_block", equity=equity, daily_loss=daily)
        self.daily_blocked = (self.daily_blocked or daily >= self.cfg.daily_loss_limit
                              if self.cfg.daily_latch else daily >= self.cfg.daily_loss_limit)
        return equity

    def _stops(self, poll: Poll) -> set:
        stopped = set()
        for sym in TRADE_SYMBOLS:
            p = self.net[sym]
            atr = poll.current_atr_1h.get(sym)
            if atr is None and p.qty:
                self._log(poll.ms, "missing_current_atr_risk_gap", symbol=sym,
                          net=p.qty, mark=poll.mark[sym])
            # 触发后即锁定：即使 mark 反弹，也要先核实/处理残仓；绝不清账假装已平。
            if (self.emergency[sym] or
                    (p.qty and atr is not None and
                     -math.copysign(1, p.qty) * (poll.mark[sym] - p.entry_px)
                     >= self.cfg.disaster_atr * atr)):
                self.emergency[sym] = True
                stopped.add(sym)
                for leg_id in self.legs:
                    if SPECS[leg_id].symbol == sym:
                        self.legs[leg_id] = None
                self._log(poll.ms, "disaster_priority", symbol=sym, net=p.qty,
                          entry=p.entry_px, mark=poll.mark[sym], current_atr=atr)
                if not p.qty:
                    self.emergency[sym] = False  # 本轮仍禁止同根重新开仓
                continue
            for leg_id, leg in self.legs.items():
                if leg is None or SPECS[leg_id].symbol != sym or leg.entry_px is None:
                    continue
                if p.qty * leg.side <= 0:  # 虚拟对冲腿并无同向已成交净仓
                    continue
                adverse = leg.side * (poll.mark[sym] - leg.entry_px)
                protected_px = (leg.entry_px + leg.side * leg.entry_px *
                                self.cfg.break_even_buffer_bps / 10_000)
                reason = None
                if leg.break_even_armed and leg.side * (poll.mark[sym] - protected_px) <= 0:
                    reason = "break_even"
                elif (self.cfg.normal_stop_atr is not None and
                      adverse <= -self.cfg.normal_stop_atr * leg.atr_at_signal):
                    reason = "normal_stop"
                if reason:
                    self.legs[leg_id] = None
                    stopped.add(sym)
                    self.stop_hold[sym] = True
                    self._log(poll.ms, reason, symbol=sym, leg=leg_id,
                              mark=poll.mark[sym], frozen_atr=leg.atr_at_signal,
                              protected_px=protected_px if reason == "break_even" else None)
                elif (not leg.break_even_armed and
                      self.cfg.break_even_trigger_atr is not None and
                      adverse >= self.cfg.break_even_trigger_atr * leg.atr_at_signal):
                    leg.break_even_armed = True
                    self._log(poll.ms, "break_even_armed", symbol=sym, leg=leg_id)
        return stopped

    def _candidate_risk(self, candidate: Mapping[str, Optional[Leg]],
                        poll: Poll, equity: float) -> tuple:
        risk_total = notional_total = 0.0
        risk_by_leg = {}
        for leg_id, leg in candidate.items():
            if leg is None:
                continue
            px = poll.mark[SPECS[leg_id].symbol]
            distance = (self.cfg.normal_stop_atr or self.cfg.disaster_atr) * leg.atr_at_signal
            risk = leg.qty * (distance + 2 * px *
                              (self.cfg.fee_per_side + self.cfg.slippage_bps / 10_000))
            risk_total += risk
            notional_total += leg.qty * px  # 不给虚拟对冲净额抵扣
            risk_by_leg[leg_id] = risk
        return risk_by_leg, risk_total, notional_total

    def _signals(self, poll: Poll, ready: Sequence[Signal], equity: float,
                 stopped: set) -> None:
        latest = {}
        for s in ready:
            if s.leg_id in latest:
                self.missed_signals += 1
                self._log(poll.ms, "missed_signal", leg=s.leg_id, close_ms=latest[s.leg_id].close_ms)
            latest[s.leg_id] = s
        for leg_id in self.cfg.enabled_legs:
            s = latest.get(leg_id)
            if s is None:
                continue
            sym = SPECS[leg_id].symbol
            if sym in stopped:
                self._log(poll.ms, "signal_suppressed_by_stop", leg=leg_id)
                continue
            if s.side and s.macd_hist is not None and s.side * s.macd_hist <= 0:
                self._log(poll.ms, "macd_divergence_discarded", leg=leg_id)
                continue
            # 旧腿止损后残留的其他虚拟腿不能在下一轮自行扩大/翻向净仓；
            # 只有新的已收盘有效信号才解除该标的止损保留态。
            self.stop_hold[sym] = False
            old = self.legs[leg_id]
            if old is not None and old.side == s.side:
                continue
            # 对侧信号先允许虚拟平仓；风控只阻新开/反手，不阻减仓。
            self.legs[leg_id] = None
            if s.side == 0:
                self._log(poll.ms, "signal_flatten", leg=leg_id)
                continue
            if self.halted or self.daily_blocked:
                self._log(poll.ms, "signal_entry_blocked", leg=leg_id, reason="account_gate")
                continue
            if sym not in poll.current_atr_1h:
                self._log(poll.ms, "signal_entry_blocked", leg=leg_id, reason="missing_current_atr")
                continue
            proposed = Leg(s.side, s.qty, s.atr_1h, s.close_ms)
            candidate = dict(self.legs)
            candidate[leg_id] = proposed
            by_leg, risk, notional = self._candidate_risk(candidate, poll, equity)
            if ((self.cfg.leg_risk_limit is not None and
                 by_leg[leg_id] > equity * self.cfg.leg_risk_limit) or
                (self.cfg.portfolio_risk_limit is not None and
                 risk > equity * self.cfg.portfolio_risk_limit) or
                (self.cfg.gross_leverage_limit is not None and
                 notional > equity * self.cfg.gross_leverage_limit)):
                self._log(poll.ms, "signal_entry_blocked", leg=leg_id,
                          reason="gross_risk_budget", risk=risk, gross_notional=notional)
                continue
            self.legs[leg_id] = proposed
            self._log(poll.ms, "signal_target", leg=leg_id, side=s.side, qty=s.qty,
                      delay_ms=poll.ms-s.close_ms)

    def _targets_and_model_fills(self, poll: Poll, stopped: set) -> None:
        for sym in TRADE_SYMBOLS:
            target = sum(leg.side * leg.qty for leg_id, leg in self.legs.items()
                         if leg is not None and SPECS[leg_id].symbol == sym)
            current = self.net[sym].qty
            if sym in stopped or self.emergency[sym] or self.stop_hold[sym]:
                if self.emergency[sym]:
                    target = 0.0
                elif target * current < 0:
                    target = 0.0  # 正常/保本止损绝不顺势翻向
                elif abs(target) > abs(current):
                    # 剩余虚拟腿请求量比已确认净仓还大：现有净仓无法归属给某一腿。
                    # 候选止损时宁可全平，也不把失去归属的残仓视作已受保护。
                    if sym in stopped:
                        self._log(poll.ms, "ambiguous_stop_allocation", symbol=sym,
                                  net=current, virtual_remaining=target)
                        target = 0.0
                    else:
                        target = current
            if self.halted or self.daily_blocked or sym not in poll.current_atr_1h:
                if target * current < 0:
                    target = 0.0
                elif abs(target) > abs(current):
                    target = current
            self.pending[sym] = target
            gap = target - current
            if abs(gap) < self.cfg.qty_step - 1e-10:
                if abs(gap) > 1e-10:
                    self._log(poll.ms, "below_step_residual", symbol=sym, residual=gap)
                continue
            if sym in poll.confirmed_fill:
                continue  # 这一轮只按逐笔成交回报，绝不再推断一笔模型成交
            fraction = poll.fill_fraction.get(sym, 0.0)  # 未提供成交证据默认零成交
            if fraction <= 0 or sym not in poll.trade_px:
                self._log(poll.ms, "unconfirmed_or_unfilled", symbol=sym, target=target,
                          net=current, residual=gap)
                continue
            size = math.floor((abs(gap) * fraction + 1e-10) / self.cfg.qty_step) * self.cfg.qty_step
            size = min(size, abs(gap))
            if size < self.cfg.qty_step - 1e-10:
                self._log(poll.ms, "below_step_residual", symbol=sym, residual=gap)
                continue
            signed = math.copysign(size, gap)
            reference = poll.trade_px[sym]
            price = reference * (1 + math.copysign(self.cfg.slippage_bps / 10_000, signed))
            fee = size * price * poll.fee_rate.get(sym, self.cfg.fee_per_side)
            self._record_fill(poll.ms, sym, signed, price, fee, source="scenario",
                              reference=reference)
            if abs(self.pending[sym] - self.net[sym].qty) >= self.cfg.qty_step - 1e-10:
                self._log(poll.ms, "partial_residual", symbol=sym,
                          target=target, net=self.net[sym].qty)
            if self.emergency[sym] and abs(self.net[sym].qty) < self.cfg.qty_step - 1e-10:
                self.emergency[sym] = False

    def run(self, signals: Sequence[Signal], polls: Sequence[Poll]) -> ReplayResult:
        if self._used:
            raise RuntimeError("每个回放实例只能运行一次")
        self._used = True
        ordered = list(signals)
        if any(ordered[i].close_ms > ordered[i + 1].close_ms for i in range(len(ordered)-1)):
            raise ValueError("信号必须按已收盘时间排序")
        for s in ordered:
            if s.leg_id not in self.legs or s.side not in (-1, 0, 1):
                raise ValueError("信号策略或方向无效/策略未启用")
            if s.side:
                _positive(s.qty, "信号数量")
                _positive(s.atr_1h, "信号已收盘 ATR")
            if s.macd_hist is not None and not math.isfinite(s.macd_hist):
                raise ValueError("MACD 能量柱须有限")
        idx, last_ms = 0, None
        for poll in polls:
            self._validate_poll(poll, last_ms)
            last_ms = poll.ms
            self._confirmed_fills(poll)  # 上一轮委托在两次轮询间成交，先对齐净仓
            self._funding(poll)
            equity = self._gates(poll)  # 先做账户闸门，但不阻既有保护退出
            stopped = self._stops(poll)  # 净仓灾难 > 腿级保本/普通 > 收盘信号
            ready = []
            while idx < len(ordered) and ordered[idx].close_ms + self.cfg.signal_delay_ms <= poll.ms:
                ready.append(ordered[idx]); idx += 1
            self._signals(poll, ready, equity, stopped)
            self._targets_and_model_fills(poll, stopped)
            equity = self._equity(poll.mark)
            self.peak = max(self.peak, equity)
            self.curve.append({"ts": poll.ms, "equity": equity, "cash": self.cash,
                               "btc_net": self.net["BTCUSDT"].qty,
                               "eth_net": self.net["ETHUSDT"].qty})
        if not self.curve:
            raise ValueError("至少需要一次双标的轮询")
        return ReplayResult(self.cash, self.curve[-1]["equity"], self.peak,
                            self.net, self.legs, self.curve, self.events, self.fees,
                            self.funding, self.slippage_cost, self.realized,
                            self.halted, self.daily_blocked, self.missed_signals,
                            self.pending)


def ohlc_path_polls(*, start_ms: int, bar_ms: int,
                    ohlc: Mapping[str, Tuple[float, float, float, float]],
                    current_atr_1h: Mapping[str, float], path: str,
                    poll_interval_ms: int = 15_000,
                    fill_fraction: Optional[Mapping[str, float]] = None) -> list:
    """OHLC 人工路径情景：O-H-L-C 或 O-L-H-C，并每 15 秒线性插值。

    这不是历史 mark 或盘口，也不证明真实高低点时间；仅用于比较同根顺序边界。
    """
    if path not in ("high_first", "low_first") or bar_ms % poll_interval_ms:
        raise ValueError("路径须 high_first/low_first，K 线长须能被轮询间隔整除")
    if bar_ms < 3 * poll_interval_ms or set(ohlc) != set(TRADE_SYMBOLS):
        raise ValueError("需要两个标的、且一根 K 线足够容纳两次穿越")
    for sym, (o, h, l, c) in ohlc.items():
        if not (0 < l <= min(o, c) <= max(o, c) <= h):
            raise ValueError(f"{sym} OHLC 不自洽")
    ticks = bar_ms // poll_interval_ms
    first, second = ticks // 3, (2 * ticks) // 3
    def price(values, k):
        o, h, l, c = values
        route = (o, h, l, c) if path == "high_first" else (o, l, h, c)
        left, right = ((0, first) if k <= first else
                       ((first, second) if k <= second else (second, ticks)))
        anchor = (0 if left == 0 else (1 if left == first else 2))
        return route[anchor] + (route[anchor+1] - route[anchor]) * (k-left) / (right-left)
    out = []
    for tick in range(ticks+1):
        marks = {s: price(ohlc[s], tick) for s in TRADE_SYMBOLS}
        out.append(Poll(start_ms + tick * poll_interval_ms, marks, current_atr_1h,
                        trade_px=marks, fill_fraction=fill_fraction or {}))
    return out
