"""每日定时任务 — 02:00 CST 结算 / 注解 / 复盘 / 日总结.

可被 PanelApp 挂到 asyncio 后台; 也支持手动 run_once().
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config.review import (
    AUTO_ACCEPT_PROPOSALS,
    DAILY_SUMMARIES_DIR,
    DAILY_SUMMARY_HOUR_CST,
)
from config.weights import DIMENSION_WEIGHTS
from review import deepseek as ds
from review import meta_review, overrides, review_loop
from review.journal import TradeJournal
from review.prompts.daily_summary import build_daily_summary_messages
from review.settle import settle_pending
from review.stats import compute_stats

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


def _next_2am_cst(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(CST)
    target = now.replace(
        hour=DAILY_SUMMARY_HOUR_CST, minute=0, second=0, microsecond=0
    )
    if now >= target:
        target = target + timedelta(days=1)
    return target


class DailyScheduler:
    def __init__(
        self,
        journal: Optional[TradeJournal] = None,
        get_client=None,  # callable → DeepSeekClient | None
    ) -> None:
        self.journal = journal if journal is not None else TradeJournal().load()
        self.get_client = get_client
        self.last_run: Optional[str] = None
        self.last_result: Dict[str, Any] = {}
        self._running = False

    async def run(self, stop_event: asyncio.Event) -> None:
        """永久循环, 等到 02:00 CST 执行."""
        self._running = True
        while not stop_event.is_set():
            nxt = _next_2am_cst()
            delay = max(1.0, (nxt - datetime.now(CST)).total_seconds())
            logger.info("scheduler: 下次日终任务 %s (%.0fs)", nxt.isoformat(), delay)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            try:
                self.last_result = await self.run_once()
                self.last_run = datetime.now(CST).strftime("%Y-%m-%d")
            except Exception as exc:
                logger.exception("scheduler run_once: %s", exc)
                self.last_result = {"ok": False, "error": str(exc)}
        self._running = False

    async def run_once(self) -> Dict[str, Any]:
        """立即执行一轮: 结算 → 注解 → 回滚检查 → 复盘 → 日总结."""
        result: Dict[str, Any] = {"ok": True, "steps": {}}
        self.journal.load(force=True)

        # 1. 结算
        try:
            settle_summary = await settle_pending(self.journal)
            result["steps"]["settle"] = settle_summary
        except Exception as exc:
            result["steps"]["settle"] = {"error": str(exc)}
            logger.warning("settle: %s", exc)

        client = None
        if self.get_client:
            try:
                client = self.get_client()
            except Exception as exc:
                result["steps"]["client"] = {"error": str(exc)}

        # 2. 错单注解
        annotated = 0
        if client is not None:
            try:
                stats = compute_stats(self.journal.all())
                for rec in self.journal.all():
                    if rec.status == "wrong" and not rec.model_note:
                        await review_loop.annotate_error(
                            client, rec, journal=self.journal,
                            recent_stats=stats.to_dict(),
                        )
                        annotated += 1
                        if annotated >= 15:  # 控 token
                            break
                result["steps"]["annotate"] = {"count": annotated}
            except Exception as exc:
                result["steps"]["annotate"] = {"error": str(exc)}

        # 3. 自动回滚检查
        state = meta_review.load_meta_state()
        try:
            should, reason, parent = meta_review.check_rollback(
                self.journal.all(), state
            )
            if should and parent:
                overrides.rollback(parent)
                state.consecutive_rollbacks += 1
                state.last_rollback_reason = reason
                state.history.append({
                    "ts": datetime.now(CST).isoformat(),
                    "event": "rollback",
                    "reason": reason,
                    "to": parent,
                })
                state = meta_review.enter_observation_if_needed(state)
                meta_review.save_meta_state(state)
                result["steps"]["rollback"] = {"did": True, "to": parent, "reason": reason}
            else:
                result["steps"]["rollback"] = {"did": False, "reason": reason}
        except Exception as exc:
            result["steps"]["rollback"] = {"error": str(exc)}

        # 4. 批次复盘 + 元评审门控 + 自动采纳
        if client is not None and AUTO_ACCEPT_PROPOSALS and not state.observation_mode:
            try:
                proposal = await review_loop.run_review(
                    client, self.journal.all(), force=False
                )
                result["steps"]["review"] = {
                    "proposal_id": proposal.proposal_id,
                    "status": proposal.status,
                    "blocked": proposal.blocked_reason,
                }
                if proposal.status == "pending" and proposal.changes:
                    current = overrides.effective_params()
                    gated, state, note = meta_review.gate_proposal(
                        proposal, current, state
                    )
                    result["steps"]["meta_gate"] = note
                    if gated.status == "pending" and gated.changes:
                        # 写回 gated 改动后采纳
                        review_loop.save_proposal(gated)
                        doc = review_loop.accept_proposal(gated.proposal_id)
                        state = meta_review.tick_locks_after_accept(state)
                        state.consecutive_rollbacks = 0
                        state.history.append({
                            "ts": datetime.now(CST).isoformat(),
                            "event": "auto_accept",
                            "version": doc.get("version"),
                            "proposal": gated.proposal_id,
                        })
                        meta_review.save_meta_state(state)
                        result["steps"]["accept"] = {
                            "version": doc.get("version"),
                            "changes": len(gated.changes),
                        }
                    else:
                        meta_review.save_meta_state(state)
                        result["steps"]["accept"] = {
                            "skipped": True,
                            "reason": gated.blocked_reason or note,
                        }
            except Exception as exc:
                result["steps"]["review"] = {"error": str(exc)}
                logger.warning("review: %s", exc)
        else:
            result["steps"]["review"] = {
                "skipped": True,
                "reason": (
                    "observation_mode" if state.observation_mode
                    else ("no_client" if client is None else "auto_accept_off")
                ),
            }

        # 5. 日总结
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
        if client is not None:
            try:
                day_start = datetime.now(CST).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                day_ms = int(day_start.timestamp() * 1000)
                day_recs = [
                    r for r in self.journal.all() if r.opened_at_ms >= day_ms
                ]
                stats = compute_stats(day_recs)
                conv = meta_review.convergence_status(self.journal.all(), state)
                weights = {
                    f"short_term.{k}": v
                    for k, v in DIMENSION_WEIGHTS["short_term"].items()
                }
                weights.update({
                    f"long_term.{k}": v
                    for k, v in DIMENSION_WEIGHTS["long_term"].items()
                })
                # 用生效参数覆盖
                eff = overrides.effective_params()
                for path, val in eff.items():
                    if path.startswith("DIMENSION_WEIGHTS."):
                        # DIMENSION_WEIGHTS.short_term.news → short_term.news
                        parts = path.split(".")
                        if len(parts) == 3:
                            weights[f"{parts[1]}.{parts[2]}"] = val

                messages = build_daily_summary_messages(
                    date_str, day_recs, stats.to_dict(), weights, conv
                )
                summary = await client.chat_json(messages)
                out = {
                    "date": date_str,
                    "generated_at": datetime.now(CST).isoformat(),
                    "summary": summary,
                    "stats": stats.to_dict(),
                    "convergence": conv,
                }
                DAILY_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
                path = DAILY_SUMMARIES_DIR / f"{date_str}.json"
                path.write_text(
                    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                result["steps"]["daily_summary"] = {"path": str(path)}
            except Exception as exc:
                result["steps"]["daily_summary"] = {"error": str(exc)}
                logger.warning("daily_summary: %s", exc)

        return result
