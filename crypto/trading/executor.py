"""自动交易执行器 — 评分信号 → 出场检查 → 仓位动作 → journal/ledger.

接入方式: PanelApp 每次 score 完成后调用 on_snapshot().
V8.3: trading_enabled=False 仍执行止损; staleness/coverage 门控新开仓;
      长期自动交易按策略合同禁用; 冷却防止损后立刻重开.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.review import STALENESS_LIMITS, TRADING_HISTORY_PATH
from config.strategy_contract import get_contract
from models.review import SettleStatus, TradeRecord, now_ms
from models.signals import ActionDecision
from review import overrides
from review.journal import TradeJournal, new_trade_id
from review.trade_ledger import TradeLedger, TradeLedgerEntry
from trading.binance_client import BinanceTestnetClient
from trading.exit_checker import ExitChecker
from trading.models import SystemHealth, TradeAction
from trading.position_manager import PositionManager
from trading.risk_guardian import RiskGuardian

logger = logging.getLogger(__name__)

ACTIONABLE = frozenset({
    ActionDecision.STRONG_LONG.value,
    ActionDecision.STANDARD_LONG.value,
    ActionDecision.STRONG_SHORT.value,
    ActionDecision.STANDARD_SHORT.value,
})


class TradeExecutor:
    def __init__(
        self,
        client: Optional[BinanceTestnetClient] = None,
        journal: Optional[TradeJournal] = None,
        history_path: Optional[Path] = None,
        guardian: Optional[RiskGuardian] = None,
    ) -> None:
        self.client = client or BinanceTestnetClient()
        # 假盘/单测严禁污染生产 positions/history/ledger
        is_fake = type(self.client).__name__ == "FakeBinanceClient"
        if is_fake:
            import tempfile
            self._test_tmp = tempfile.TemporaryDirectory(prefix="crypto_fake_")
            fake_root = Path(self._test_tmp.name)
            self.manager = PositionManager(
                self.client, persist_path=fake_root / "positions.json"
            )
            self.ledger = TradeLedger(fake_root / "trade_ledger.jsonl")
            self.history_path = Path(history_path) if history_path else (
                fake_root / "trading_history.jsonl"
            )
            self.journal = journal if journal is not None else TradeJournal(
                path=fake_root / "journal.jsonl"
            ).load()
        else:
            self.manager = PositionManager(self.client)
            self.journal = journal if journal is not None else TradeJournal().load()
            self.ledger = TradeLedger()
            self.history_path = Path(history_path) if history_path else TRADING_HISTORY_PATH
        self.enabled = False  # 默认关闭 — 工程验收前不自动下单
        self.last_actions: List[dict] = []
        self.last_error: str = ""
        self._history: List[dict] = []
        self._load_history()
        self.exit_checker = ExitChecker()
        self._prev_predict_fun: Dict[str, Optional[float]] = {
            "short_term": None,
            "long_term": None,
        }
        self._cooldown_until: Dict[str, float] = {
            "short_term": 0.0,
            "long_term": 0.0,
        }
        self.guardian = guardian
        if self.guardian is None:
            self.guardian = RiskGuardian(self.client, self.manager)

    def _load_history(self) -> None:
        self._history = []
        if not self.history_path.exists():
            return
        try:
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                self._history.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("trading history load: %s", exc)

    def _append_history(self, row: dict) -> None:
        self._history.append(row)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def run_guardian(self, stop) -> None:
        """风控轮询入口：Guardian 动作统一走成交账本，禁止只写 _actions."""
        import asyncio
        logger.info("executor.run_guardian started")
        if self.guardian:
            await self.guardian.reconcile_protections()
        while not stop.is_set():
            try:
                acts = await self.guardian.tick() if self.guardian else []
                for act in acts:
                    shell = type("Snap", (), {})()
                    # 关联原持仓配置，不用空 guardian_tick 冒充
                    pos = self.manager.get_position(getattr(act, "horizon", "")) if act else None
                    shell.config_snapshot = dict(getattr(pos, "config_snapshot", None) or {
                        "version": "position_exit",
                        "content_hash": None,
                        "params": {},
                        "exit_source": "guardian",
                    })
                    shell.composite_score = None
                    shell.partial_cs = None
                    await self._record_action(act, shell)
                self._update_daily_pnl_from_ledger()
            except Exception as exc:
                logger.exception("run_guardian: %s", exc)
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=float(getattr(self.guardian, "interval_sec", 2.0) or 2.0),
                )
            except Exception:
                pass

    def _update_daily_pnl_from_ledger(self) -> None:
        """从真实账本累计已实现盈亏 → Guardian 日内风险。入金/出金不计入。"""
        if not self.guardian:
            return
        realized = 0.0
        for r in self.ledger.recent(500):
            if r.get("entry_kind") != "fill":
                continue
            if r.get("realized_pnl_usdt") is not None:
                realized += float(r["realized_pnl_usdt"])
        # 相对名义权益（假盘/测试用 client.equity）
        eq = 10000.0
        try:
            # 同步属性优先
            eq = float(getattr(self.client, "equity", None) or getattr(self.client, "_equity", 10000) or 10000)
        except Exception:
            pass
        pct = realized / eq if eq else 0.0
        self.guardian.note_daily_pnl(pct)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def _face_confidences(self, snap: Any) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for key, attr in (
            ("news", "news_detail"),
            ("data", "data_detail"),
            ("tech", "tech_detail"),
            ("prediction", "prediction_detail"),
        ):
            detail = getattr(snap, attr, None)
            if detail is not None:
                out[key] = float(getattr(detail, "confidence", 1.0) or 0.0)
            else:
                out[key] = 1.0
        return out

    def _atr_from_snap(self, snap: Any) -> tuple:
        atr = getattr(snap, "atr", None)
        atr_mean = getattr(snap, "atr_mean", None)
        atr_pct = getattr(snap, "atr_pct", None)
        if atr is None:
            tech = getattr(snap, "tech_detail", None)
            if tech is not None:
                atr = getattr(tech, "atr", None)
                atr_pct = atr_pct or getattr(tech, "atr_pct", None)
        if (atr is None or atr <= 0) and atr_pct and getattr(snap, "mark_price", None):
            atr = float(atr_pct) * float(snap.mark_price)
        return atr, atr_mean

    def _staleness_blocks_entry(self, snap: Any) -> Optional[str]:
        stale_map = getattr(snap, "staleness_sec", None) or {}
        for source, age in stale_map.items():
            if age is None:
                continue
            limit = float(STALENESS_LIMITS.get(source, STALENESS_LIMITS.get("default", 300)))
            if float(age) > limit:
                if self.guardian:
                    if self.guardian.health == SystemHealth.NORMAL:
                        self.guardian.health = SystemHealth.DEGRADED
                return f"stale:{source}={age:.0f}s>{limit:.0f}s"
        return None

    async def _run_exits(
        self, snap: Any, horizon: str, price: float, cs: float, atr
    ) -> List[TradeAction]:
        actions: List[TradeAction] = []
        pos = self.manager.get_position(horizon)
        if pos is None or pos.is_flat():
            return actions
        pf = getattr(snap, "predict_fun_up_prob", None)
        prev_pf = self._prev_predict_fun.get(horizon)
        exit_acts = self.exit_checker.check_exits(
            pos,
            price,
            cs,
            now_ms=int(time.time() * 1000),
            spread_vs_mean=getattr(snap, "spread_vs_mean", None),
            liq_5m_usd=getattr(snap, "liq_5m_usd", None),
            predict_fun_up_prob=pf,
            prev_predict_fun_up_prob=prev_pf,
            atr=atr,
        )
        for ea in exit_acts:
            tact = await self.manager.apply_exit(horizon, ea, price, decision="EXIT")
            if tact:
                actions.append(tact)
                await self._record_action(tact, snap)
                if ea.kind == "full_close" or tact.action in ("close", "reverse_close"):
                    contract = get_contract(horizon)
                    self._cooldown_until[horizon] = time.time() + contract.cooldown_after_exit_sec
                    if self.guardian:
                        await self.guardian.cancel_protection(horizon)
                elif self.guardian:
                    await self.guardian.ensure_protection(horizon)
        return actions

    async def on_snapshot(self, snap: Any, horizon: str) -> List[TradeAction]:
        """处理一帧评分快照. horizon: short_term | long_term."""
        if not self.configured:
            self.last_error = "binance_keys_missing"
            return []

        # 评分失败通知 guardian (仍可独立保护)
        if self.guardian:
            self.guardian.note_score_result(True)

        price = float(getattr(snap, "mark_price", None) or 0)
        if price <= 0:
            try:
                price = await self.client.mark_price()
            except Exception as exc:
                self.last_error = str(exc)
                if self.guardian:
                    self.guardian.note_score_result(False)
                return []

        cs = float(
            getattr(snap, "composite_score", None)
            or getattr(snap, "partial_cs", None)
            or 0
        )
        atr, atr_mean = self._atr_from_snap(snap)
        actions: List[TradeAction] = []

        # 无论 enabled 与否: 先跑出场 / 止损
        exit_actions = await self._run_exits(snap, horizon, price, cs, atr)
        actions.extend(exit_actions)
        pos_after = self.manager.get_position(horizon)
        if pos_after is None or pos_after.is_flat():
            if exit_actions:
                self._prev_predict_fun[horizon] = getattr(snap, "predict_fun_up_prob", None)
                self.last_actions = [a.to_dict() for a in actions]
                self.last_error = ""
                return actions

        # trading_enabled=False → 只减仓, 禁止新开
        if not self.enabled:
            self.last_actions = [a.to_dict() for a in actions]
            self.last_error = "trading_disabled_exits_only"
            return actions

        # 黑天鹅覆盖 → 强制平仓
        if getattr(snap, "overridden", False):
            for h in ("short_term", "long_term"):
                act = await self.manager.force_close(h)
                if act:
                    actions.append(act)
                    await self._record_action(act, snap)
            self.last_actions = [a.to_dict() for a in actions]
            return actions

        # 策略合同: 长期自动交易暂禁
        contract = get_contract(horizon)
        if not contract.auto_trade_enabled:
            self.last_error = contract.observe_only_reason or "observe_only"
            self.last_actions = [a.to_dict() for a in actions]
            return actions

        # 对账 / 健康 / tradable / staleness
        if self.manager.reconciliation_needed:
            self.last_error = "reconciliation_needed"
            return actions
        if self.guardian and not self.guardian.allow_new_entries():
            self.last_error = f"health={self.guardian.health.value}"
            return actions
        if self.guardian and self.guardian.last_mark_ts <= 0:
            self.last_error = "guardian_not_ready"
            return actions
        if not hasattr(snap, "tradable"):
            self.last_error = "missing_tradable_field"
            return actions
        if snap.tradable is False:
            self.last_error = getattr(snap, "reject_reason", None) or "not_tradable"
            return actions
        # DataRecord 准入：mark_price 必须 VALID；清算等观察项缺失不挡开仓
        records = getattr(snap, "data_records", None)
        if isinstance(records, dict) and "mark_price" in records:
            from trading.models import DataValidity
            rec = records.get("mark_price")
            if rec is not None and getattr(rec, "validity", DataValidity.VALID) != DataValidity.VALID:
                self.last_error = f"invalid_data_record:mark_price:{getattr(rec.validity, 'value', rec.validity)}"
                return actions
            if rec is not None and getattr(rec, "usable", True) is False:
                self.last_error = "invalid_data_record:mark_price"
                return actions
        stale_reason = self._staleness_blocks_entry(snap)
        if stale_reason:
            self.last_error = stale_reason
            return actions
        if time.time() < self._cooldown_until.get(horizon, 0):
            self.last_error = "cooldown"
            return actions

        decision = snap.decision
        if hasattr(decision, "value"):
            decision = decision.value
        decision = str(decision)

        # 成交前复核：用执行场所新鲜 mark，禁止用信号价平移目标掩盖追价
        from trading.pretrade import PretradeLimits, recheck_entry
        side = None
        if decision in ("STRONG_LONG", "STANDARD_LONG"):
            side = "LONG"
        elif decision in ("STRONG_SHORT", "STANDARD_SHORT"):
            side = "SHORT"
        if side:
            signal_px = float(getattr(snap, "mark_price", None) or price)
            signal_ts = int(getattr(snap, "timestamp_ms", None) or 0)
            now = int(time.time() * 1000)
            try:
                exec_mark = float(await self.client.mark_price())
            except Exception as exc:
                self.last_error = f"pretrade_mark_failed:{exc}"
                return actions
            quote_ts = now  # client.mark_price 即时拉取
            cfg_snap = getattr(snap, "config_snapshot", None) or {}
            exit_cfg = (cfg_snap.get("exit_strategy") or {}).get(horizon) or {}
            hard_sl_atr = float(exit_cfg.get("hard_sl_atr") or (1.0 if horizon == "short_term" else 2.0))
            tp1_atr = float(exit_cfg.get("tp1_atr") or (1.2 if horizon == "short_term" else 2.0))
            atr_v = float(atr) if atr else None
            sl_dist = (atr_v * hard_sl_atr) if atr_v else None
            tp_dist = (atr_v * tp1_atr) if atr_v else None
            pre = recheck_entry(
                side=side,
                signal_price=signal_px,
                signal_ts_ms=signal_ts or now,
                now_ms=now,
                exec_mark=exec_mark,
                quote_ts_ms=quote_ts,
                atr=atr_v,
                hard_sl_distance=sl_dist,
                tp_distance=tp_dist,
                limits=PretradeLimits(),
            )
            if not pre.ok:
                self.last_error = "pretrade:" + ",".join(pre.reasons)
                logger.info("pretrade block %s %s", horizon, pre.reasons)
                return actions
            price = exec_mark

        confs = self._face_confidences(snap)
        min_conf = min(confs.values()) if confs else 1.0

        try:
            signal_actions = await self.manager.on_signal(
                horizon,
                decision,
                price,
                cs=cs,
                min_confidence=min_conf,
                atr=atr,
                atr_mean=atr_mean,
                entry_cs=cs,
            )
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("executor on_signal: %s", exc)
            if self.guardian:
                self.guardian.note_score_result(False)
            return actions

        for act in signal_actions:
            actions.append(act)
            await self._record_action(act, snap)
            if act.action in ("open", "reverse_open") and self.guardian:
                await self.guardian.ensure_protection(horizon)

        self._prev_predict_fun[horizon] = getattr(snap, "predict_fun_up_prob", None)
        self.last_actions = [a.to_dict() for a in actions]
        self.last_error = ""
        return actions

    async def _record_action(self, act: TradeAction, snap: Any) -> None:
        # 配置快照：优先用决策/持仓钉死的，禁止事后热读盖章
        cfg_snap = getattr(snap, "config_snapshot", None) or {}
        if not isinstance(cfg_snap, dict):
            cfg_snap = {}
        # Guardian 退出：关联原持仓配置
        pos = self.manager.get_position(act.horizon) if act.horizon else None
        if (not cfg_snap.get("version") or cfg_snap.get("version") == "guardian_tick") and pos and getattr(pos, "config_snapshot", None):
            cfg_snap = dict(pos.config_snapshot or {})
        cfg_ver = cfg_snap.get("version") or getattr(snap, "config_version", None)
        if not cfg_ver or not isinstance(cfg_ver, str) or cfg_ver == "guardian_tick":
            # 仍未知时标 audit，不用 ACTIVE 冒充决策版本
            cfg_ver = cfg_snap.get("version") or "unknown_decision_config"
        ch = cfg_snap.get("content_hash")
        row = {
            "ts_ms": now_ms(),
            **act.to_dict(),
            "cs": getattr(snap, "composite_score", None)
            or getattr(snap, "partial_cs", None),
            "config_version": cfg_ver,
            "content_hash": ch if isinstance(ch, (str, type(None))) else None,
        }
        # 字段齐全后再落盘（禁止先写再改字典）
        self._append_history(row)

        # 真实成交账本：仅确认成交；拒单写审计
        if act.action in ("open", "close", "partial_close", "reverse_close", "reverse_open"):
            order_ok = bool(act.order and act.order.ok)
            is_internal = False
            for a in self.manager.last_allocations:
                if a.horizon == act.horizon and a.is_internal_match:
                    is_internal = True
            if not order_ok and not (act.order and act.order.status == "SYNCED"):
                self.ledger.append(TradeLedgerEntry(
                    entry_id=TradeLedger.new_id(),
                    trade_id=act.trade_id or "",
                    ts_ms=now_ms(),
                    horizon=act.horizon,
                    action=act.action,
                    side=act.side,
                    quantity=0.0,
                    price=act.price,
                    fee_usdt=0.0,
                    fee_unknown=True,
                    is_internal_match=False,
                    rejected=True,
                    is_audit=True,
                    entry_kind="reject",
                    order_id=(act.order.order_id if act.order else ""),
                    client_order_id=(act.order.client_order_id if act.order else ""),
                    note=act.reason or (act.order.error if act.order else "rejected"),
                    config_version=cfg_ver,
                    content_hash=row.get("content_hash"),
                ))
            else:
                filled = float(act.quantity or 0)
                if act.order and getattr(act.order, "cum_filled_qty", None):
                    filled = float(act.order.cum_filled_qty)
                fee_unknown = not is_internal  # 无真实费用源时标未知
                self.ledger.append(TradeLedgerEntry(
                    entry_id=TradeLedger.new_id(),
                    trade_id=act.trade_id or "",
                    ts_ms=now_ms(),
                    horizon=act.horizon,
                    action=act.action,
                    side=act.side,
                    quantity=filled,
                    price=act.price,
                    fee_usdt=0.0,
                    fee_unknown=fee_unknown and filled > 0,
                    is_internal_match=is_internal,
                    rejected=False,
                    is_audit=False,
                    entry_kind="fill" if filled > 0 or is_internal else "sync",
                    order_id=(act.order.order_id if act.order else ""),
                    client_order_id=(act.order.client_order_id if act.order else ""),
                    note=act.reason,
                    config_version=cfg_ver,
                    content_hash=row.get("content_hash"),
                ))

        if act.action in ("open", "reverse_open") and act.order and act.order.ok:
            scores = {
                "news": float(getattr(snap, "s_news", None) or 0),
                "data": float(getattr(snap, "s_data", None) or 0),
                "tech": float(getattr(snap, "s_tech", None) or 0),
                "prediction": float(getattr(snap, "s_prediction", None) or 0),
            }
            decision = act.decision
            if decision not in ACTIONABLE and decision not in (
                "STRONG_LONG", "STANDARD_LONG", "STRONG_SHORT", "STANDARD_SHORT"
            ):
                return
            opened = now_ms()
            rec = TradeRecord(
                trade_id=new_trade_id(opened),
                opened_at_ms=opened,
                horizon=act.horizon,
                entry_price=act.price,
                scores=scores,
                composite_score=float(
                    getattr(snap, "composite_score", None)
                    or getattr(snap, "partial_cs", None)
                    or 0
                ),
                decision=decision,
                atr=getattr(snap, "atr", None),
                safety_valve=bool(getattr(snap, "safety_valve", False)),
                overridden=bool(getattr(snap, "overridden", False)),
                missing_dimensions=list(getattr(snap, "missing_dimensions", []) or []),
                source="auto",
                note=(
                    f"auto {act.action} lev={act.leverage}x qty={act.quantity} "
                    f"risk={getattr(self.manager.get_position(act.horizon), 'risk_usdt', None)}"
                ),
                config_version=cfg_ver,
                status=SettleStatus.PENDING.value,
            )
            self.journal.append(rec)
            pos = self.manager.get_position(act.horizon)
            if pos:
                pos.trade_id = rec.trade_id
            act.trade_id = rec.trade_id

    async def status(self) -> Dict[str, Any]:
        await self.manager.refresh_marks()
        bal = None
        exch_pos = None
        connected = False
        err = self.last_error
        if self.configured:
            try:
                bal = (await self.client.get_balance()).to_dict()
                exch_pos = (await self.client.get_position()).to_dict()
                connected = True
            except Exception as exc:
                err = str(exc)
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "connected": connected,
            "error": err,
            "positions": self.manager.snapshot(),
            "balance": bal,
            "exchange_position": exch_pos,
            "last_actions": self.last_actions[-5:],
            "leverage": dict(self.manager.leverage),
            "reconciliation_needed": self.manager.reconciliation_needed,
            "guardian": self.guardian.status() if self.guardian else None,
            "cooldown_until": dict(self._cooldown_until),
        }

    async def flatten_orphan(self) -> dict:
        """本地空仓时平掉交易所孤立仓并解除 reconciliation_needed."""
        res = await self.manager.flatten_orphan_exchange()
        return {
            "ok": bool(res.ok or res.status == "FLAT"),
            "status": res.status,
            "error": res.error,
            "quantity": res.quantity,
            "avg_price": res.avg_price,
            "reconciliation_needed": self.manager.reconciliation_needed,
            "allow_new_entries": self.manager._allow_new_entries,
        }

    async def force_close(self, horizon: str) -> Optional[dict]:
        act = await self.manager.force_close(horizon)
        if act:
            shell = type("Snap", (), {})()
            pos = self.manager.get_position(horizon)
            shell.config_snapshot = dict(getattr(pos, "config_snapshot", None) or {
                "version": "manual_force_close",
                "content_hash": None,
                "params": {},
                "exit_source": "manual",
            })
            shell.composite_score = None
            shell.partial_cs = None
            await self._record_action(act, shell)
            self._update_daily_pnl_from_ledger()
            closed_ok = bool(act.order and (act.order.ok or act.order.status in ("FLAT", "SYNCED", "EXTERNAL_FLAT")))
            still = self.manager.get_position(horizon)
            if self.guardian:
                if closed_ok and (still is None or still.is_flat()):
                    await self.guardian.release_protection_if_flat(
                        not self.manager.active_positions()
                    )
                elif not closed_ok or (still and not still.is_flat()):
                    # 部分成交/拒绝：保留保护
                    await self.guardian.ensure_protection(horizon)
            return act.to_dict()
        return None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def history(self, limit: int = 20) -> List[dict]:
        return list(reversed(self._history[-limit:]))

    async def close(self) -> None:
        await self.client.close()
