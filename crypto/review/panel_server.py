"""本机面板后端 — 对齐 btc-four-face-monitor.html 全部 API.

启动:
  python3 -m review.panel_server
  → http://127.0.0.1:8787/

职责:
  * 服务 HTML + /api/live (四面实时 CS)
  * 复盘档案 / 结算 / 统计调参建议 / 配置版本
  * Binance 模拟盘交易 + 自我递归改进
  * 密钥优先读 runtime/secrets.json / 环境变量, 面板可改写
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

from config.review import (
    PANEL_HOST,
    PANEL_HTML,
    PANEL_PORT,
    SETTLE_CONFIG,
    VALID_SAMPLE_TARGET,
)
from config.secrets import load_secrets, save_secrets, mask_secret
from config.strategy_registry import load_registry, upsert_strategy
from engine.scorer import FactorScoringEngine
from models.review import SettleStatus, TradeRecord, now_ms
from models.signals import DimensionScores, StrategyHorizon
from review import meta_review, overrides, review_loop
from review.journal import TradeJournal, new_trade_id
from review.settle import settle_pending
from review.stats import compute_stats
from runtime.panel_server import PanelApp, snapshot_to_panel
from runtime.scheduler import DailyScheduler
from trading.binance_client import BinanceTestnetClient
from trading.executor import TradeExecutor
from trading.runtime_mode import startup_status

logger = logging.getLogger(__name__)


class ReviewPanelState:
    """复盘侧状态: 档案 + 交易执行器.

    复盘由 review.statistical_review 纯算法驱动, 不需要任何 API Key,
    因此本状态里不存在模型客户端。
    """

    def __init__(self) -> None:
        self.journal = TradeJournal().load()
        self.executor = TradeExecutor(
            client=BinanceTestnetClient(),
            journal=self.journal,
        )
        self.scheduler = DailyScheduler(journal=self.journal)
        overrides.ensure_baseline()

    async def close(self) -> None:
        await self.executor.close()


def create_app(
    live: Optional[PanelApp] = None,
    review: Optional[ReviewPanelState] = None,
) -> "web.Application":
    if web is None:
        raise RuntimeError("aiohttp required")

    review = review or ReviewPanelState()
    live = live or PanelApp(executor=review.executor)
    if live.executor is None:
        live.executor = review.executor
    paper_account: Dict[str, Any] = {"position": None, "forced_flat": False}
    @web.middleware
    async def local_frontend_cors(request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
        origin = request.headers.get("Origin", "")
        if origin in {"http://127.0.0.1:8788", "http://localhost:8788"}:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    app = web.Application(middlewares=[local_frontend_cors])
    app["live"] = live
    app["review"] = review

    async def on_start(_app):
        await live.start_scoring()
        # 日终调度后台
        app["scheduler_task"] = asyncio.create_task(
            review.scheduler.run(live._stop)
        )

    async def on_stop(_app):
        await live.stop_scoring()
        t = app.get("scheduler_task")
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await review.close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)

    # ------------------------------------------------------------------ 静态
    async def index(_request):
        if not PANEL_HTML.exists():
            return web.Response(text="panel html missing", status=404)
        return web.FileResponse(PANEL_HTML)

    # ------------------------------------------------------------------ live
    async def api_live(request):
        horizon = request.rel_url.query.get("horizon", "short")
        hz = (
            StrategyHorizon.LONG_TERM
            if horizon in ("long", "long_term")
            else StrategyHorizon.SHORT_TERM
        )
        snap = live.latest_for(horizon)
        if snap is None:
            # 暖机中不阻塞 HTTP — 由后台评分环产出首帧
            return web.json_response(
                {"live": False, "error": "warming_up"},
                status=503,
            )
        if hz == StrategyHorizon.LONG_TERM and live.loop_long.latest is not None:
            snap = live.loop_long.latest
        return web.json_response(snapshot_to_panel(snap, hz))

    async def api_market(_request):
        """返回 Binance WS 采集器的最新标记价格，不依赖评分环暖机。"""
        snap = live.loop_short.binance.get_snapshot()
        if snap.mark_price is None:
            return web.json_response({"ok": False, "error": "market_warming_up"}, status=503)
        return web.json_response({
            "ok": True,
            "symbol": "BTCUSDT",
            "mark_price": snap.mark_price,
            "index_price": snap.index_price,
            "event_time_ms": snap.event_time_ms,
            "funding_rate_annualized": snap.funding_rate_annualized,
            "best_bid": snap.best_bid,
            "best_ask": snap.best_ask,
            "source": "binance_futures_websocket",
        })

    async def api_health(_request):
        s = live.loop_short.latest
        trading = {
            "trading_configured": review.executor.configured,
            "trading_connected": False,
            "trading_enabled": review.executor.enabled,
        }
        try:
            ts = await asyncio.wait_for(review.executor.status(), timeout=3.0)
            trading = {
                "trading_configured": bool(ts.get("configured")),
                "trading_connected": bool(ts.get("connected")),
                "trading_enabled": bool(ts.get("enabled")),
            }
        except Exception:
            pass
        return web.json_response({
            "ok": True,
            "has_snapshot": s is not None,
            "full_cs": bool(s and s.is_full_cs),
            "journal_records": len(review.journal),
            "review_engine": review_loop.ENGINE_NAME,
            **trading,
        })

    # ------------------------------------------------------------------ journal
    async def api_journal_list(request):
        status = request.rel_url.query.get("status")
        horizon = request.rel_url.query.get("horizon")
        limit = request.rel_url.query.get("limit")
        errors_only = request.rel_url.query.get("errors_only") == "1"
        lim = int(limit) if limit else None
        recs = review.journal.filter(
            status=status, horizon=horizon, limit=lim, errors_only=errors_only
        )
        return web.json_response({"records": [r.to_dict() for r in recs]})

    async def api_journal_add(request):
        body = await request.json()
        scores = body.get("scores") or {}
        horizon = body.get("horizon") or "short_term"
        entry = float(body.get("entry_price") or 0)
        if entry <= 0:
            return web.json_response({"ok": False, "error": "entry_price required"}, status=400)

        # 用当前引擎复算 CS, 避免人工填错
        dim = DimensionScores(
            news=float(scores.get("news") or 0),
            data=float(scores.get("data") or 0),
            tech=float(scores.get("tech") or 0),
            prediction=float(scores.get("prediction") or 0),
        )
        hz = (
            StrategyHorizon.LONG_TERM
            if horizon == "long_term"
            else StrategyHorizon.SHORT_TERM
        )
        eng = FactorScoringEngine()
        # 应用生效配置
        overrides.apply_to_engine(eng)
        ev = eng.evaluate(hz, dim)

        opened = int(body.get("opened_at_ms") or now_ms())
        rec = TradeRecord(
            trade_id=new_trade_id(opened),
            opened_at_ms=opened,
            horizon=hz.value,
            entry_price=entry,
            scores={
                "news": dim.news,
                "data": dim.data,
                "tech": dim.tech,
                "prediction": dim.prediction,
            },
            composite_score=ev.composite_score,
            decision=ev.decision.value,
            atr=body.get("atr"),
            safety_valve=ev.safety_valve_triggered,
            overridden=bool(body.get("overridden")),
            source=body.get("source") or "manual",
            note=body.get("note") or "",
            config_version=overrides.current_version_label(),
        )
        # 观望/中性 → 排除
        if rec.overridden or rec.decision in (
            "NEUTRAL", "WATCH_LONG", "WATCH_SHORT"
        ):
            rec.status = SettleStatus.EXCLUDED.value
            rec.settle_detail = {
                "reason": "overridden" if rec.overridden else (
                    "neutral_no_trade" if rec.decision == "NEUTRAL" else "watch_no_position"
                )
            }
        review.journal.append(rec)
        return web.json_response({"ok": True, "record": rec.to_dict()})

    # ------------------------------------------------------------------ stats / config
    async def api_stats(_request):
        stats = compute_stats(review.journal.all())
        return web.json_response({"stats": stats.to_dict()})

    async def api_config(_request):
        secrets = load_secrets()
        bn_key = secrets.get("binance_testnet_api_key") or ""
        pf_key = secrets.get("predict_fun_api_key") or ""
        return web.json_response({
            "review_engine": review_loop.ENGINE_NAME,
            "valid_sample_target": VALID_SAMPLE_TARGET,
            "settle": SETTLE_CONFIG,
            "config_version": overrides.current_version_label(),
            "binance_configured": bool(bn_key and secrets.get("binance_testnet_api_secret")),
            "binance_key_masked": mask_secret(bn_key) if bn_key else "",
            "binance_base_url": secrets.get("binance_testnet_base_url")
                or "https://testnet.binancefuture.com",
            "predict_fun_configured": bool(pf_key),
            "predict_fun_key_masked": mask_secret(pf_key) if pf_key else "",
            "runtime": startup_status(),
        })

    # ------------------------------------------------------------------ strategy registry
    async def api_strategies(_request):
        return web.json_response({"strategies": load_registry()})

    async def api_strategy_add(request):
        try:
            row = upsert_strategy(await request.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "strategy": row})

    # ------------------------------------------------------------------ settle / review
    async def api_settle_run(_request):
        summary = await settle_pending(review.journal)
        return web.json_response({"ok": True, **summary})

    async def api_review_run(request):
        body = {}
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = {}
        force = bool(body.get("force"))
        proposal = await review_loop.run_review(review.journal.all(), force=force)
        return web.json_response({"ok": True, "proposal": proposal.to_dict()})

    # ------------------------------------------------------------------ proposals / versions
    async def api_proposals(_request):
        return web.json_response({
            "proposals": [p.to_dict() for p in review_loop.load_proposals()]
        })

    async def api_proposal_accept(request):
        pid = request.match_info["pid"]
        try:
            result = review_loop.accept_proposal(pid)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, **result})

    async def api_proposal_reject(request):
        pid = request.match_info["pid"]
        try:
            p = review_loop.reject_proposal(pid)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "proposal": p.to_dict()})

    async def api_versions(_request):
        return web.json_response({
            "active": overrides.active_version_name() or overrides.current_version_label(),
            "versions": overrides.list_versions(),
        })

    async def api_versions_rollback(request):
        body = await request.json()
        ver = body.get("version")
        if not ver:
            return web.json_response({"ok": False, "error": "version required"}, status=400)
        try:
            doc = overrides.rollback(ver)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "version": doc})

    # ------------------------------------------------------------------ trading
    async def api_trading_status(_request):
        status = await review.executor.status()
        if os.getenv("TRADING_MODE", "paper").lower() == "paper" and paper_account["forced_flat"]:
            status["exchange_position"] = {"symbol": "BTCUSDT", "side": "FLAT", "quantity": 0.0, "position_amt": 0.0}
            status["positions"] = {"short_term": None, "long_term": None}
        elif os.getenv("TRADING_MODE", "paper").lower() == "paper" and paper_account["position"]:
            status["exchange_position"] = dict(paper_account["position"])
        return web.json_response(status)

    async def api_trading_flatten_orphan(_request):
        out = await review.executor.flatten_orphan()
        return web.json_response(out)

    async def api_trading_close(request):
        body = await request.json()
        horizon = body.get("horizon") or "short_term"
        if horizon not in ("short_term", "long_term"):
            return web.json_response({"ok": False, "error": "bad horizon"}, status=400)
        act = await review.executor.force_close(horizon)
        return web.json_response({"ok": True, "action": act})

    async def api_testnet_limit_then_close(request):
        """受控 Testnet 冒烟测试：限价入场后立即 reduceOnly 平仓。

        限价单改走 client.place_limit_order（提交后轮询确认），
        不再把刚提交的 NEW 状态误判为失败并取消。
        无论成功与否，结束前都会重新对账，避免留下陈旧的开仓阻塞。
        """
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"ok": False, "stage": "preflight", "error": "binance_keys_missing"},
                status=400,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        qty = float((body or {}).get("quantity") or 0.001)
        if qty != 0.001:
            return web.json_response(
                {"ok": False, "stage": "preflight",
                 "error": "smoke test only permits quantity=0.001 BTC"},
                status=400,
            )
        try:
            before = await client.get_position()
            if abs(float(getattr(before, "quantity", 0) or 0)) > 1e-12:
                return web.json_response({
                    "ok": False, "stage": "preflight",
                    "error": "account is not flat; refusing to mix with existing position",
                    "position_before": {"side": before.side, "quantity": before.quantity},
                }, status=409)
            mark = float(await client.mark_price())
            # 限价追价: 先被动挂单争取 maker 手续费, 未成交则逐档追价直至成交
            entry = await client.place_limit_chase("LONG", qty)
            filled = float(entry.cum_filled_qty or 0)
            if filled <= 0:
                recon = await review.executor.reconcile(
                    reason="testnet_smoke_entry_unfilled"
                )
                after_unfilled = await client.get_position()
                return web.json_response({
                    "ok": False, "stage": "entry",
                    "error": "limit_not_filled",
                    "entry_order": entry.__dict__,
                    "mark_at_submit": mark,
                    "position_after": {
                        "side": after_unfilled.side,
                        "quantity": after_unfilled.quantity,
                    },
                    "reconciliation": recon,
                }, status=409)
            close = await client.place_limit_chase(
                "SHORT", filled, reduce_only=True
            )
            after = await client.get_position()
            flat = abs(float(getattr(after, "quantity", 0) or 0)) <= 1e-12
            recon = await review.executor.reconcile(reason="testnet_smoke_done")
            return web.json_response({
                "ok": bool(close.ok and flat),
                "stage": "done",
                "entry_order": entry.__dict__,
                "close_order": close.__dict__,
                "filled_qty": filled,
                "position_before": {"side": before.side, "quantity": before.quantity},
                "position_after": {"side": after.side, "quantity": after.quantity},
                "entry_avg_price": entry.avg_price or 0,
                "mark_at_submit": mark,
                "flat_verified": flat,
                "reconciliation": recon,
            }, status=200 if (close.ok and flat) else 502)
        except Exception as exc:
            recon = None
            try:
                recon = await review.executor.reconcile(reason="testnet_smoke_error")
            except Exception:
                pass
            return web.json_response({
                "ok": False, "stage": "exception",
                "error": f"{type(exc).__name__}: {exc}",
                "reconciliation": recon,
            }, status=502)

    async def api_testnet_flatten_now(_request):
        """平掉当前观测到的 Testnet BTCUSDT 仓位，并强制重新对账。

        平仓后必须对账，否则本地账本会与交易所脱节并永久阻塞开仓。
        """
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"ok": False, "stage": "preflight", "error": "binance_keys_missing"},
                status=400,
            )
        try:
            pos = await client.get_position()
            qty = float(getattr(pos, "quantity", 0) or 0)
            side = str(getattr(pos, "side", "FLAT") or "FLAT").upper()
            if qty <= 0 or side == "FLAT":
                recon = await review.executor.reconcile(
                    reason="flatten_now_already_flat"
                )
                return web.json_response({
                    "ok": True, "already_flat": True, "stage": "done",
                    "position_after": {"side": "FLAT", "quantity": 0.0},
                    "reconciliation": recon,
                })
            close = await client.place_limit_chase(
                "SHORT" if side == "LONG" else "LONG", qty, reduce_only=True
            )
            after = await client.get_position()
            flat = abs(float(getattr(after, "quantity", 0) or 0)) <= 1e-12
            recon = await review.executor.reconcile(reason="flatten_now_done")
            return web.json_response({
                "ok": bool(close.ok and flat),
                "stage": "done",
                "close_order": close.__dict__,
                "position_before": {
                    "side": side, "quantity": qty,
                    "entry_price": getattr(pos, "entry_price", 0),
                },
                "position_after": {"side": after.side, "quantity": after.quantity},
                "flat_verified": flat,
                "reconciliation": recon,
            }, status=200 if (close.ok and flat) else 502)
        except Exception as exc:
            recon = None
            try:
                recon = await review.executor.reconcile(reason="flatten_now_error")
            except Exception:
                pass
            return web.json_response({
                "ok": False, "stage": "exception",
                "error": f"{type(exc).__name__}: {exc}",
                "reconciliation": recon,
            }, status=502)

    async def api_trading_toggle(request):
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        review.executor.set_enabled(enabled)
        return web.json_response({"ok": True, "enabled": review.executor.enabled})

    async def api_trading_reconcile(request):
        """从交易所实时状态重新对账，用于清除陈旧的开仓阻塞。

        与重启的区别：可在运行期调用，返回结构化判定结果。
        不一致时保持阻塞并在 stage/error 中说明原因，不假装成功。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = str((body or {}).get("reason") or "manual")
        try:
            result = await review.executor.reconcile(reason=reason)
        except Exception as exc:
            return web.json_response({
                "ok": False, "stage": "exception",
                "error": f"{type(exc).__name__}: {exc}",
            }, status=502)
        return web.json_response(result)

    async def api_trading_history(request):
        limit = int(request.rel_url.query.get("limit") or 20)
        return web.json_response({"history": review.executor.history(limit=limit)})

    async def api_paper_test_order(request):
        """Paper-only order rehearsal; never calls Binance order endpoints."""
        if os.getenv("TRADING_MODE", "paper").lower() != "paper":
            return web.json_response({"ok": False, "error": "paper_test_requires_TRADING_MODE_paper"}, status=400)
        body = await request.json()
        side = str(body.get("side") or "LONG").upper()
        if side not in ("LONG", "SHORT"):
            return web.json_response({"ok": False, "error": "side must be LONG or SHORT"}, status=400)
        market = live.loop_short.binance.get_snapshot()
        mark = float(market.mark_price or 0)
        limit_price = float(body.get("limit_price") or mark)
        notional = float(body.get("notional_usdt") or 100)
        leverage = max(1, min(int(body.get("leverage") or 2), 20))
        if limit_price <= 0 or notional <= 0:
            return web.json_response({"ok": False, "error": "limit_price and notional_usdt must be positive"}, status=400)
        qty = round(notional * leverage / limit_price, 3)
        if qty <= 0:
            return web.json_response({"ok": False, "error": "calculated quantity is zero"}, status=400)
        default_tp = limit_price * (1.005 if side == "LONG" else 0.995)
        default_sl = limit_price * (0.997 if side == "LONG" else 1.003)
        tp = float(body.get("take_profit_price") or default_tp)
        sl = float(body.get("stop_loss_price") or default_sl)
        valid = (side == "LONG" and sl < limit_price < tp) or (side == "SHORT" and tp < limit_price < sl)
        if not valid:
            return web.json_response({"ok": False, "error": "LONG requires SL < limit < TP; SHORT requires TP < limit < SL"}, status=400)
        order_side = "BUY" if side == "LONG" else "SELL"
        exit_side = "SELL" if side == "LONG" else "BUY"
        paper_account["position"] = {
            "symbol": "BTCUSDT", "side": side, "quantity": qty,
            "position_amt": qty if side == "LONG" else -qty,
            "entry_price": round(limit_price, 2), "mark_price": round(mark or limit_price, 2),
        }
        paper_account["forced_flat"] = False
        return web.json_response({
            "ok": True,
            "mode": "paper",
            "exchange_calls": 0,
            "filled": True,
            "symbol": "BTCUSDT",
            "position": {"side": side, "quantity": qty, "entry_price": round(limit_price, 2), "notional_usdt": round(qty * limit_price, 2), "leverage": leverage},
            "entry_order": {"type": "LIMIT", "side": order_side, "price": round(limit_price, 2), "quantity": qty, "time_in_force": "GTC"},
            "take_profit_order": {"type": "TAKE_PROFIT_MARKET", "side": exit_side, "trigger_price": round(tp, 2), "reduce_only": True, "close_position": True},
            "stop_loss_order": {"type": "STOP_MARKET", "side": exit_side, "trigger_price": round(sl, 2), "reduce_only": True, "close_position": True},
            "note": "模拟成交与保护单编排已通过；未向 Binance 发送任何订单。",
        })

    async def api_paper_test_close(request):
        """Paper-only automatic close rehearsal for TP/SL trigger handling."""
        if os.getenv("TRADING_MODE", "paper").lower() != "paper":
            return web.json_response({"ok": False, "error": "paper_test_requires_TRADING_MODE_paper"}, status=400)
        body = await request.json()
        side = str(body.get("side") or "LONG").upper()
        reason = str(body.get("reason") or "TAKE_PROFIT").upper()
        if side not in ("LONG", "SHORT") or reason not in ("TAKE_PROFIT", "STOP_LOSS"):
            return web.json_response({"ok": False, "error": "side must be LONG/SHORT and reason TAKE_PROFIT/STOP_LOSS"}, status=400)
        entry = float(body.get("entry_price") or 83500)
        qty = float(body.get("quantity") or 0.002)
        move = 0.005 if reason == "TAKE_PROFIT" else -0.003
        if side == "SHORT":
            move = -move
        exit_price = float(body.get("exit_price") or entry * (1 + move))
        if entry <= 0 or qty <= 0 or exit_price <= 0:
            return web.json_response({"ok": False, "error": "entry_price, exit_price and quantity must be positive"}, status=400)
        close_side = "SELL" if side == "LONG" else "BUY"
        pnl = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
        paper_account["position"] = None
        paper_account["forced_flat"] = True
        return web.json_response({
            "ok": True,
            "mode": "paper",
            "exchange_calls": 0,
            "position_before": {"side": side, "quantity": qty, "entry_price": round(entry, 2)},
            "trigger": {"reason": reason, "trigger_price": round(exit_price, 2)},
            "close_order": {"type": "MARKET", "side": close_side, "quantity": qty, "reduce_only": True, "close_position": True, "status": "FILLED"},
            "position_after": {"side": "FLAT", "quantity": 0, "exit_price": round(exit_price, 2)},
            "realized_pnl_usdt": round(pnl, 6),
            "note": "模拟自动平仓已完成，仓位已归零；未向 Binance 发送任何订单。",
        })

    async def api_trading_verify(_request):
        try:
            info = await review.executor.client.verify()
            return web.json_response({"ok": True, **info})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def api_binance_keys(request):
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        api_secret = (body.get("api_secret") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        if not api_key or not api_secret:
            return web.json_response(
                {"ok": False, "error": "api_key and api_secret required"}, status=400
            )
        save_secrets({
            "binance_testnet_api_key": api_key,
            "binance_testnet_api_secret": api_secret,
            **({"binance_testnet_base_url": base_url} if base_url else {}),
        })
        # 热切换客户端
        await review.executor.client.close()
        review.executor.client = BinanceTestnetClient(
            api_key=api_key,
            api_secret=api_secret,
            base_url=base_url or None,
        )
        review.executor.manager.client = review.executor.client
        try:
            info = await review.executor.client.verify()
            recon = None
            try:
                recon = await review.executor.reconcile(reason="keys_rebound")
            except Exception as exc:
                recon = {"ok": False, "stage": "exception", "error": str(exc)}
            return web.json_response({"ok": True, **info, "reconciliation": recon})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def api_predict_fun_key(request):
        body = await request.json()
        key = (body.get("api_key") or "").strip()
        if not key:
            return web.json_response({"ok": False, "error": "api_key empty"}, status=400)
        # 快速校验 mainnet
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                url = "https://api.predict.fun/v1/markets"
                params = {"marketVariant": "CRYPTO_UP_DOWN", "status": "OPEN", "first": 1}
                async with session.get(
                    url, params=params, headers={"x-api-key": key}
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        return web.json_response(
                            {"ok": False, "error": f"Predict.fun {resp.status}: {text[:200]}"},
                            status=400,
                        )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        save_secrets({"predict_fun_api_key": key})
        # 热切换采集器到 mainnet
        try:
            live.loop_short.predict_fun.api_key = key
            live.loop_short.predict_fun.use_mainnet = True
            from config.mapping import PREDICT_FUN_MAINNET_URL
            live.loop_short.predict_fun.base_url = PREDICT_FUN_MAINNET_URL
            live.loop_short.predict_fun._snapshot.source = "mainnet"
            await live.loop_short.predict_fun.fetch_once()
        except Exception as exc:
            logger.warning("predict_fun hot-reload: %s", exc)
        return web.json_response({
            "ok": True,
            "key_masked": mask_secret(key),
            "source": "mainnet",
        })

    # ------------------------------------------------------------------ convergence / daily
    async def api_convergence(_request):
        return web.json_response(
            meta_review.convergence_status(review.journal.all())
        )

    async def api_scheduler_run(_request):
        result = await review.scheduler.run_once()
        return web.json_response({"ok": True, **result})

    async def api_daily_summaries(_request):
        from config.review import DAILY_SUMMARIES_DIR
        out = []
        if DAILY_SUMMARIES_DIR.exists():
            for f in sorted(DAILY_SUMMARIES_DIR.glob("*.json"), reverse=True)[:30]:
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    continue
        return web.json_response({"summaries": out})

    async def api_strategies_active(_request):
        """当前运行的策略定义 + 实时运行态 (数组结构, 为多策略并列预留).

        定义取自 config/strategies/*.json; 运行态取自
        runtime/shadow/deployed_state.json。仅 enabled 的策略附带运行态。
        """
        from pathlib import Path as _P
        root = _P(__file__).resolve().parents[1]
        strat_dir = root / "config" / "strategies"
        state_path = root / "runtime" / "shadow" / "deployed_state.json"
        live = {}
        try:
            if state_path.exists():
                live = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            live = {}
        out = []
        try:
            for f in sorted(strat_dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                out.append({**d, "runtime": live if d.get("enabled") else None})
        except Exception:
            pass
        return web.json_response({"strategies": out})

    async def api_binance_orders(request):
        """币安历史委托 —— 以交易所为准, 非本地账本."""
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing", "orders": []}
            )
        try:
            limit = int(request.query.get("limit", 50))
        except Exception:
            limit = 50
        try:
            orders = await client.all_orders(limit=limit)
            return web.json_response({"connected": True, "orders": orders})
        except Exception as exc:
            return web.json_response({
                "connected": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "orders": [],
            })

    async def api_binance_trades(request):
        """币安历史成交 —— 含 realizedPnl 与 commission, 是盈亏的唯一可信来源."""
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing", "trades": []}
            )
        try:
            limit = int(request.query.get("limit", 50))
        except Exception:
            limit = 50
        try:
            trades = await client.user_trades(limit=limit)
            return web.json_response({"connected": True, "trades": trades})
        except Exception as exc:
            return web.json_response({
                "connected": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "trades": [],
            })

    async def api_account_summary(_request):
        """账户汇总: 余额 + 已实现/未实现盈亏 + 手续费 + 胜率与盈亏比."""
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing"}
            )
        try:
            bal = await client.get_balance()
            pos = await client.get_position()
            trades = []
            try:
                trades = await client.user_trades(limit=200)
            except Exception:
                trades = []
            pnls = [float(t.get("realizedPnl", 0) or 0) for t in trades]
            realized = sum(pnls)
            commission = sum(float(t.get("commission", 0) or 0) for t in trades)
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            gross_win = sum(p for p in pnls if p > 0)
            gross_loss = -sum(p for p in pnls if p < 0)
            return web.json_response({
                "connected": True,
                "wallet_balance": float(
                    getattr(bal, "total_wallet_balance", 0) or 0
                ),
                "available_balance": float(
                    getattr(bal, "available_balance", 0) or 0
                ),
                "unrealized_pnl": float(getattr(pos, "unrealized_pnl", 0) or 0),
                "realized_pnl": realized,
                "commission": commission,
                "net_pnl": realized - commission,
                "trade_count": len(trades),
                "wins": wins,
                "losses": losses,
                "win_rate": (wins / (wins + losses)) if (wins + losses) else 0.0,
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else 0.0,
                "position": {
                    "side": str(getattr(pos, "side", "FLAT") or "FLAT"),
                    "quantity": float(getattr(pos, "quantity", 0) or 0),
                    "entry_price": float(getattr(pos, "entry_price", 0) or 0),
                },
            })
        except Exception as exc:
            return web.json_response({
                "connected": False, "reason": f"{type(exc).__name__}: {exc}"
            })

    # routes
    app.router.add_get("/", index)
    app.router.add_get("/api/live", api_live)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/journal", api_journal_list)
    app.router.add_post("/api/journal", api_journal_add)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/strategies", api_strategies)
    app.router.add_get("/api/strategies/active", api_strategies_active)
    app.router.add_get("/api/binance/orders", api_binance_orders)
    app.router.add_get("/api/binance/trades", api_binance_trades)
    app.router.add_get("/api/account/summary", api_account_summary)
    app.router.add_post("/api/strategies", api_strategy_add)
    app.router.add_post("/api/settle/run", api_settle_run)
    app.router.add_post("/api/review/run", api_review_run)
    app.router.add_get("/api/proposals", api_proposals)
    app.router.add_post("/api/proposals/{pid}/accept", api_proposal_accept)
    app.router.add_post("/api/proposals/{pid}/reject", api_proposal_reject)
    app.router.add_get("/api/versions", api_versions)
    app.router.add_post("/api/versions/rollback", api_versions_rollback)
    app.router.add_get("/api/trading/status", api_trading_status)
    app.router.add_post("/api/trading/flatten_orphan", api_trading_flatten_orphan)
    app.router.add_post("/api/trading/reconcile", api_trading_reconcile)
    app.router.add_post("/api/trading/close", api_trading_close)
    app.router.add_post("/api/testnet/smoke-limit-close", api_testnet_limit_then_close)
    app.router.add_post("/api/testnet/flatten-now", api_testnet_flatten_now)
    app.router.add_post("/api/trading/toggle", api_trading_toggle)
    app.router.add_get("/api/trading/history", api_trading_history)
    app.router.add_post("/api/paper/test-order", api_paper_test_order)
    app.router.add_post("/api/paper/test-close", api_paper_test_close)
    app.router.add_post("/api/trading/verify", api_trading_verify)
    app.router.add_post("/api/trading/keys", api_binance_keys)
    app.router.add_post("/api/predict_fun/key", api_predict_fun_key)
    app.router.add_get("/api/review/convergence", api_convergence)
    app.router.add_post("/api/scheduler/run", api_scheduler_run)
    app.router.add_get("/api/daily_summaries", api_daily_summaries)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if web is None:
        raise SystemExit("pip install aiohttp")
    try:
        app = create_app()
        logger.info("panel http://%s:%s/  (HTML=%s)", PANEL_HOST, PANEL_PORT, PANEL_HTML)
        web.run_app(app, host=PANEL_HOST, port=PANEL_PORT, print=lambda *_: None)
    except KeyboardInterrupt:
        logger.info("panel stopped by KeyboardInterrupt")
    except Exception:
        logger.exception("panel crashed")
        raise


if __name__ == "__main__":
    main()
