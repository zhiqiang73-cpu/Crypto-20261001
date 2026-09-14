"""自动交易执行器 — 评分信号 → 出场检查 → 仓位动作 → journal.

接入方式: PanelApp 每次 score 完成后调用 on_snapshot().
V8: 先跑 ExitChecker (TP/SL/追踪/时间/CS衰减/异常), 再跑信号驱动开平.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.review import TRADING_HISTORY_PATH
from models.review import SettleStatus, TradeRecord, now_ms
from models.signals import ActionDecision
from review import overrides
from review.journal import TradeJournal, new_trade_id
from trading.binance_client import BinanceTestnetClient
from trading.exit_checker import ExitChecker
from trading.models import TradeAction
from trading.position_manager import PositionManager

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
    ) -> None:
        self.client = client or BinanceTestnetClient()
        self.manager = PositionManager(self.client)
        self.journal = journal if journal is not None else TradeJournal().load()
        self.history_path = Path(history_path) if history_path else TRADING_HISTORY_PATH
        self.enabled = True
        self.last_actions: List[dict] = []
        self.last_error: str = ""
        self._history: List[dict] = []
        self._load_history()
        self.exit_checker = ExitChecker()
        self._prev_predict_fun: Dict[str, Optional[float]] = {
            "short_term": None,
            "long_term": None,
        }

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
        # atr_pct × mark → 绝对 ATR 兜底
        if (atr is None or atr <= 0) and atr_pct and getattr(snap, "mark_price", None):
            atr = float(atr_pct) * float(snap.mark_price)
        return atr, atr_mean

    async def on_snapshot(self, snap: Any, horizon: str) -> List[TradeAction]:
        """处理一帧评分快照. horizon: short_term | long_term."""
        if not self.enabled:
            return []
        if not self.configured:
            self.last_error = "binance_keys_missing"
            return []
        if getattr(snap, "overridden", False):
            actions = []
            for h in ("short_term", "long_term"):
                act = await self.manager.force_close(h)
                if act:
                    actions.append(act)
                    await self._record_action(act, snap)
            self.last_actions = [a.to_dict() for a in actions]
            return actions

        decision = snap.decision
        if hasattr(decision, "value"):
            decision = decision.value
        decision = str(decision)
        price = float(getattr(snap, "mark_price", None) or 0)
        if price <= 0:
            try:
                price = await self.client.mark_price()
            except Exception as exc:
                self.last_error = str(exc)
                return []

        cs = float(
            getattr(snap, "composite_score", None)
            or getattr(snap, "partial_cs", None)
            or 0
        )
        actions: List[TradeAction] = []

        # ---- V8: 先检查持仓出场 ----
        pos = self.manager.get_position(horizon)
        atr, atr_mean = self._atr_from_snap(snap)
        if pos and not pos.is_flat():
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
                tact = await self.manager.apply_exit(horizon, ea, price, decision)
                if tact:
                    actions.append(tact)
                    await self._record_action(tact, snap)
            # 全平后不再跑开仓信号 (同一帧)
            pos_after = self.manager.get_position(horizon)
            if pos_after is None or pos_after.is_flat():
                self._prev_predict_fun[horizon] = pf
                self.last_actions = [a.to_dict() for a in actions]
                self.last_error = ""
                return actions
            # 有减仓/收紧动作时, 本帧仍可继续看信号 (同向持有会 no-op)

        # ---- 信号驱动开平 ----
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
            return actions

        for act in signal_actions:
            actions.append(act)
            await self._record_action(act, snap)

        self._prev_predict_fun[horizon] = getattr(snap, "predict_fun_up_prob", None)
        self.last_actions = [a.to_dict() for a in actions]
        self.last_error = ""
        return actions

    async def _record_action(self, act: TradeAction, snap: Any) -> None:
        row = {
            "ts_ms": now_ms(),
            **act.to_dict(),
            "cs": getattr(snap, "composite_score", None)
            or getattr(snap, "partial_cs", None),
            "config_version": overrides.current_version_label(),
        }
        self._append_history(row)

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
                config_version=overrides.current_version_label(),
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
        }

    async def force_close(self, horizon: str) -> Optional[dict]:
        act = await self.manager.force_close(horizon)
        if act:
            self._append_history({"ts_ms": now_ms(), **act.to_dict(), "source": "manual"})
            return act.to_dict()
        return None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def history(self, limit: int = 20) -> List[dict]:
        return list(reversed(self._history[-limit:]))

    async def close(self) -> None:
        await self.client.close()
