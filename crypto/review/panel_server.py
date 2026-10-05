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
import csv
import datetime
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# ---------------------------------------------------------------------------
# 记录起点（用户要求：历史清零，系统从某一刻起重新记录）
#
# 优先级：config/record_start.json > 环境变量 STATS_START_DATE > 默认值。
# 时间按本机时区（北京时间 UTC+8）解释，支持 "YYYY-MM-DD" 或
# "YYYY-MM-DD HH:MM"，精确到分钟。
#
# ⚠ 交易所的委托/成交记录无法删除，只能按起点过滤显示；本地运行日志另行
#   归档重置（见 runtime/shadow/_archive_reset_*）。持仓、余额、挂单是
#   实时状态，不受记录起点影响。
#
# 历史教训：起算日曾经只作用于统计指标，而委托/成交复用它会「凭空消失」。
# 现在的语义是明确的「记录起点」：用户要求页面上从此刻起只显示新记录，
# 因此 /api/binance/orders 与 /api/binance/trades 也按同一起点过滤。
# ---------------------------------------------------------------------------
# 净仓对齐死区：与 shadow/deploy.py::SYNC_MIN_DELTA 同口径（2 个最小步长）。
# 步长取整会让账本与交易所偶尔差一个步长，那种差值不构成「不属于同一笔仓」。
MIN_SYNC_STEP = 0.002

# 账本缓存：完整持仓周期要按 ≤7 天分窗拉多页成交与资金费，若每次刷新都全量
# 重拉会明显加重 REST 负担（此前已因轮询过密被交易所限流）。默认 30 秒内复用。
LEDGER_TTL_MS = 30_000
_LEDGER_CACHE: Dict[str, Any] = {"at_ms": 0, "data": None}

DEFAULT_RECORD_START = "2026-10-02"
RECORD_START_FILE = os.path.join(ROOT, "config", "record_start.json")


def record_start_text() -> str:
    """记录起点原文：配置文件优先，其次环境变量，最后默认值。"""
    try:
        with open(RECORD_START_FILE, encoding="utf-8") as fh:
            value = str((json.load(fh) or {}).get("record_start") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return os.getenv("STATS_START_DATE", DEFAULT_RECORD_START)


def parse_local_ms(text: str) -> int:
    """本机时区 "YYYY-MM-DD[ HH:MM[:SS]]" → 毫秒；无法解析返回 0。"""
    from datetime import datetime as _dt

    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(_dt.strptime(raw, fmt).timestamp() * 1000)
        except Exception:
            continue
    return 0


def stats_start_ms() -> int:
    """记录起点的毫秒时间戳；无法解析时返回 0，表示不过滤。"""
    return parse_local_ms(record_start_text())


def record_start_label() -> str:
    """记录起点的展示文案（北京时间）。"""
    ms = stats_start_ms()
    if not ms:
        return "不限"
    from datetime import datetime as _dt

    return _dt.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _trade_row_ms(row: Dict[str, Any]) -> int:
    """运行记录 deployed_trades.csv 的「时间」列 → 毫秒。

    ⚠ 该列由运行器 shadow/deploy.py 的 _fmt() 写成 **UTC**
    （datetime.fromtimestamp(..., tz=timezone.utc)），不是北京时间。
    面板内部统一用毫秒时间戳比较，展示时才换算为北京时间；这里必须按
    UTC 解析，否则北京时间会被当成 UTC，记录起点的过滤会整体偏移 8 小时。
    """
    from datetime import datetime as _dt, timezone as _tz

    raw = str(row.get("时间") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(
                _dt.strptime(raw, fmt).replace(tzinfo=_tz.utc).timestamp() * 1000
            )
        except Exception:
            continue
    return 0


def _quality_context(start_ms: int = 0) -> Dict[str, Any]:
    """读取本地运行记录，提供前端风险质量卡的可审计上下文。

    这里只读 runtime/shadow；不修改状态、不清理日志，也不把观察行冒充成交。
    start_ms > 0 时只统计记录起点之后的行，使回撤与原因记录从起点重新累计。
    """
    from collections import Counter

    state_path = os.path.join(ROOT, "runtime", "shadow", "deployed_state.json")
    trade_path = os.path.join(ROOT, "runtime", "shadow", "deployed_trades.csv")
    heartbeat_path = os.path.join(ROOT, "runtime", "shadow", "runner_heartbeat.json")
    state: Dict[str, Any] = {}
    heartbeat: Dict[str, Any] = {}
    reasons: Counter[str] = Counter()
    equity: list[float] = []
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        pass
    try:
        with open(heartbeat_path, encoding="utf-8") as fh:
            heartbeat = json.load(fh)
    except Exception:
        pass
    try:
        with open(trade_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
            if start_ms:
                rows = [r for r in rows if _trade_row_ms(r) >= start_ms]
            for row in rows[-300:]:
                note = str(row.get("说明") or "").strip()
                if note and note not in {"观察", "同向持仓"}:
                    reasons[note] += 1
                try:
                    value = float(str(row.get("权益") or "").replace(",", ""))
                    if value > 0:
                        equity.append(value)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    max_drawdown = 0.0
    peak = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    strategies = [str(x) for x in (heartbeat.get("strategies") or [])]
    return {
        "recorded_max_drawdown": max_drawdown,
        "recorded_equity_points": len(equity),
        # 日亏额度与峰值：从运行器状态文件带出，供前端风控状态条使用。
        "day_start_eq": float(state.get("day_start_eq") or 0) or None,
        "peak": float(state.get("peak") or 0) or None,
        "recent_reasons": [
            {"text": text, "count": count}
            for text, count in reasons.most_common(4)
        ],
        "active_strategies": strategies,
        # 2026-10-04: 原判据是 `"5" in x`，而 kdj15/eth15 里也含字符 5，
        # 于是 5m 已停用时该标志仍为 False（误报「5m 还在跑」）。
        # 现在问的是「运行进程上报的策略里，有没有 5m 的 id」。
        "five_minute_disabled": not any(
            s in FIVE_MINUTE_STRATEGY_IDS for s in strategies
        ),
        "halted": bool(state.get("halted")),
        "missed_bars": int(state.get("missed_bars") or 0),
        "missed_signals": int(state.get("missed_signals") or 0),
        "source": "runtime/shadow/deployed_state.json + deployed_trades.csv + runner_heartbeat.json",
    }


def shadow_ledger_claim(symbol: str, exchange_signed: float) -> str:
    """交易所这笔净仓是否属于 shadow runner 的策略账本？属于则返回原因。

    2026-10-04：面板的 legacy executor 只认它自己的账本，看不到 runner 的仓位，
    因此把 runner 的 BTC/ETH 仓当成「孤儿仓」。面板上点「清孤儿仓 / 立即平仓」
    会真的把策略仓平掉。

    这里只做**只读**归属判定，证据来自 runner 自己的状态文件：
      方向一致 且 数量差 ≤ 2 个最小步长  → 认定归 runner，面板不得平。
    证据不足返回空串（不阻断），以免误伤面板自己的仓。

    判定刻意保守：宁可不拦，也不误判方向相反的仓位。
    """
    if abs(exchange_signed) < 1e-9:
        return ""
    path = os.path.join(ROOT, "runtime", "shadow", "deployed_state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:  # noqa: BLE001  读不到就不拦（保底不误伤）
        return ""
    try:
        ledger = float((desired_nets(st) or {}).get(symbol) or 0.0)
    except Exception:  # noqa: BLE001
        return ""
    if abs(ledger) < 1e-9 or ledger * exchange_signed <= 0:
        return ""
    if abs(ledger - exchange_signed) > 2 * MIN_SYNC_STEP:
        return ""
    return (f"{symbol} 这笔净仓归 shadow runner 策略账本"
            f"（账本 {ledger:+.4f} / 交易所 {exchange_signed:+.4f}）。"
            f"面板不会平掉策略仓；要平请走运行器流程或人工确认。")


def _slippage_estimate(positions: Optional[Dict[str, Any]] = None,
                       start_ms: int = 0) -> Dict[str, Any]:
    """执行滑点：交易所实际成交价 vs 策略账本参考价。

    只读本机 runtime/shadow，不改交易链路。两条口径，逐笔精确配对，
    绝不用时间猜测把不相干的成交和信号硬凑在一起：

    1. 台账口径：deployed_orders.jsonl 里带 signal_price 的委托
       （下单时写入的虚拟仓参考价）逐笔对比成交均价。
    2. 持仓口径：当前持仓用交易所 entry_price 对比账本 entry.px，
       这是此刻真实存在的执行偏差，可立即核对。

    两条都拿不到时返回 None，并在口径说明里写清原因，不编造数字。
    start_ms > 0 时只统计记录起点之后的持仓，避免把清零前的开仓滑点算进新账。
    """
    total = 0.0
    matched = 0
    considered = 0

    # 1) 台账口径（逐笔精确）
    try:
        with open(
            os.path.join(ROOT, "runtime", "shadow", "deployed_orders.jsonl"),
            encoding="utf-8",
        ) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                action = str(o.get("action") or "")
                if "LONG" not in action and "SHORT" not in action:
                    continue
                try:
                    fill = float(o.get("avg_price") or 0)
                    qty = float(o.get("filled") or 0)
                    ref = float(o.get("signal_price") or 0)
                except (TypeError, ValueError):
                    continue
                if fill <= 0 or qty <= 0:
                    continue
                considered += 1
                if ref <= 0:
                    continue
                direction = 1 if "LONG" in action else -1
                total += (fill - ref) * direction * qty
                matched += 1
    except Exception:  # noqa: BLE001
        pass

    if matched:
        return {
            "slippage_est": total,
            "slippage_matched": matched,
            "slippage_candidates": considered,
            "slippage_basis": (
                f"台账逐笔口径：成交均价 − 策略参考价（{matched}/{considered} 笔）"
            ),
        }

    # 2) 持仓口径（当前持仓，交易所 entry_price vs 账本 entry.px）
    state: Dict[str, Any] = {}
    try:
        with open(
            os.path.join(ROOT, "runtime", "shadow", "deployed_state.json"),
            encoding="utf-8",
        ) as fh:
            state = json.load(fh)
    except Exception:  # noqa: BLE001
        state = {}
    books = state.get("strategies") or {}
    hold_total = 0.0
    hold_matched = 0
    for symbol in TRADE_SYMBOLS:
        pos = (positions or {}).get(symbol) or {}
        try:
            ex_px = float(pos.get("entry_price") or 0)
            ex_qty = abs(float(pos.get("quantity") or 0))
        except (TypeError, ValueError):
            continue
        if ex_px <= 0 or ex_qty <= 0:
            continue
        ref_px = 0.0
        ref_qty = 0.0
        for book in books.values():
            entry = book.get("entry") or {}
            if str(entry.get("symbol") or "") != symbol:
                continue
            try:
                q = abs(float(entry.get("qty") or 0))
                p = float(entry.get("px") or 0)
                entry_ms = int(entry.get("ms") or 0)
            except (TypeError, ValueError):
                continue
            if start_ms and entry_ms and entry_ms < start_ms:
                # 清零前开的仓不计入新账（持仓本身保留）。
                continue
            if q > 0 and p > 0:
                ref_px += p * q
                ref_qty += q
        if ref_qty <= 0:
            continue
        ref = ref_px / ref_qty
        side = str(pos.get("side") or "FLAT").upper()
        direction = 1 if side == "LONG" else (-1 if side == "SHORT" else 0)
        if not direction:
            continue
        hold_total += (ex_px - ref) * direction * ex_qty
        hold_matched += 1

    if hold_matched:
        return {
            "slippage_est": hold_total,
            "slippage_matched": hold_matched,
            "slippage_candidates": hold_matched,
            "slippage_basis": (
                f"持仓口径：交易所开仓价 − 账本参考价（{hold_matched} 个标的，"
                "逐笔台账参考价自下次运行器重启后开始记录）"
            ),
        }

    return {
        "slippage_est": None,
        "slippage_matched": 0,
        "slippage_candidates": considered,
        "slippage_basis": (
            f"暂不可测：记录起点之后暂无带参考价的台账成交（台账 {considered} 笔）"
            "，也没有起点之后新开的持仓可比对"
        ),
    }


try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

from config.review import (
    PANEL_HOST,
    PANEL_HTML,
    PANEL_PORT,
    PANEL_STATIC_DIR,
    SETTLE_CONFIG,
    VALID_SAMPLE_TARGET,
)
from config.secrets import load_secrets, save_secrets, mask_secret
from trading.mainnet_readonly import (
    MAINNET_USDM_BASE,
    MainnetReadOnlyError,
    verify_usdm_credentials_readonly,
)
from config.strategy_registry import load_registry, upsert_strategy
from engine.scorer import FactorScoringEngine
from models.review import SettleStatus, TradeRecord, now_ms
from models.signals import DimensionScores, StrategyHorizon
from review import cycle_pnl, meta_review, overrides, review_loop
from review.order_sources import (SOURCE_LABELS, annotate_orders, annotate_trades,
                                  load_strategy_order_ids, summarize)
from shadow.strategy_books import (SPEC_5M, SPEC_ETH_5M, TRADE_SYMBOLS,
                                   desired_net, desired_nets, position_sources,
                                   runtime_view)
# 5m 策略的 id 取自规格定义（SPEC_5M / SPEC_ETH_5M 保留定义但不在 SPECS 里，
# 表示「已停用」）。不要用 `"5" in strategy_id` 之类的字符串包含判断：
# kdj15 / eth15 里也含字符 5。
FIVE_MINUTE_STRATEGY_IDS = (SPEC_5M.id, SPEC_ETH_5M.id)
from shadow.external_watch import request_resume as request_external_resume
from shadow.external_watch import summarize as external_summary
from review.journal import TradeJournal, new_trade_id
from review.settle import settle_pending
from review.stats import compute_stats
from runtime.panel_server import PanelApp, snapshot_to_panel
from runtime.scheduler import DailyScheduler
from trading.binance_client import BinanceTestnetClient
from trading.executor import TradeExecutor
from trading.runtime_mode import startup_status

logger = logging.getLogger(__name__)


async def _rows_for_symbols(client, method: str, limit: int) -> list:
    """按标的分别拉历史，再拼成一张表。某一个失败不影响另一个。"""
    rows = []
    for symbol in TRADE_SYMBOLS:
        try:
            part = await getattr(client, method)(symbol=symbol, limit=limit)
            if part:
                rows.extend(part)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s %s: %s", method, symbol, exc)
    return rows


def _with_reading_series(rec: Dict[str, Any]) -> Dict[str, Any]:
    """读数图需要 K/D 序列。快照里没有时，用与运行器相同的测试网 K 线补上。"""
    series = rec.get("series") or {}
    if series.get("k") and series.get("d"):
        return rec
    try:
        from shadow.live import closed_kdj_series
        extra = closed_kdj_series(
            rec.get("interval") or "15m",
            symbol=rec.get("symbol") or "BTCUSDT",
        )
        rec["series"] = {"k": extra.get("k") or [], "d": extra.get("d") or []}
        if rec.get("gold") is None:
            rec["gold"] = extra.get("gold")
        if rec.get("dead") is None:
            rec["dead"] = extra.get("dead")
    except Exception as exc:  # noqa: BLE001
        logger.warning("reading series: %s", exc)
    return rec


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
        # 面板接口是实时账户状态，绝不能被浏览器缓存：
        # 否则页面可能显示上一次请求的仓位/盈亏/干预状态（已实际踩到）。
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
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

    async def static_asset(request):
        """前端静态资源与 /api/* 同源，避免跨端口与缓存不一致。"""
        name = os.path.basename(request.match_info.get("name") or "")
        allowed = {"app.js", "chart.js", "styles.css", "index.html"}
        if name not in allowed:
            return web.Response(text="not found", status=404)
        path = PANEL_STATIC_DIR / name
        if not path.exists():
            return web.Response(text="not found", status=404)
        return web.FileResponse(path)

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
        # 市场标识随行情一起下发 —— 前端据此连 WS, 不再硬编码主网地址。
        from config.market_endpoints import market_report

        mk = market_report()
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
            "market": mk["market"],
            "market_label": mk["market_label"],
            "market_ws": mk["ws"] + "/stream",
            "market_rest": mk["rest"],
            "account_base_url": mk["account_base_url"],
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
        mn_key = secrets.get("binance_mainnet_api_key") or ""
        mn_configured = bool(
            mn_key and secrets.get("binance_mainnet_api_secret")
        )
        return web.json_response({
            "review_engine": review_loop.ENGINE_NAME,
            "valid_sample_target": VALID_SAMPLE_TARGET,
            "settle": SETTLE_CONFIG,
            "config_version": overrides.current_version_label(),
            "binance_configured": bool(bn_key and secrets.get("binance_testnet_api_secret")),
            "binance_key_masked": mask_secret(bn_key) if bn_key else "",
            "binance_base_url": secrets.get("binance_testnet_base_url")
                or "https://testnet.binancefuture.com",
            # 主网凭据状态：只读验收用。主网执行仍由 runtime_mode 硬阻断。
            "mainnet_configured": mn_configured,
            "mainnet_key_masked": mask_secret(mn_key) if mn_key else "",
            "mainnet_base_url": MAINNET_USDM_BASE,
            "mainnet_execution_enabled": False,
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
        # `get_position()` 在空仓时默认 leverage=1，不能拿来展示账户的真实
        # 10x/逐仓设置。额外读 positionRisk 的设置，避免前端和交易所对不上。
        client = review.executor.client
        if client.configured:
            try:
                settings = await client.get_position_settings()
                status["exchange_settings"] = settings
                exch = status.get("exchange_position")
                if isinstance(exch, dict):
                    exch["leverage"] = settings.get("leverage", 0)
                    exch["margin_type"] = settings.get("margin_type", "UNKNOWN")
            except Exception as exc:  # noqa: BLE001
                status["exchange_settings_error"] = f"{type(exc).__name__}: {exc}"
        if os.getenv("TRADING_MODE", "paper").lower() == "paper" and paper_account["forced_flat"]:
            status["exchange_position"] = {"symbol": "BTCUSDT", "side": "FLAT", "quantity": 0.0, "position_amt": 0.0}
            status["positions"] = {"short_term": None, "long_term": None}
        elif os.getenv("TRADING_MODE", "paper").lower() == "paper" and paper_account["position"]:
            status["exchange_position"] = dict(paper_account["position"])
        state_path = os.path.join(ROOT, "runtime", "shadow", "deployed_state.json")
        trade_log = os.path.join(ROOT, "runtime", "shadow", "deployed_trades.csv")
        live = {}
        try:
            if os.path.exists(state_path):
                with open(state_path, encoding="utf-8") as fh:
                    live = json.load(fh)
        except Exception:
            live = {}
        status["position_sources"] = position_sources(live, trade_log_path=trade_log)
        status["desired_net"] = desired_net(live) if live else 0.0
        status["desired_nets"] = desired_nets(live) if live else {}
        exchange_positions = {}
        open_orders = []
        if client.configured:
            for symbol in TRADE_SYMBOLS:
                try:
                    pos = await client.get_position(symbol)
                    payload = pos.to_dict() if hasattr(pos, "to_dict") else {
                        "symbol": symbol,
                        "side": getattr(pos, "side", "FLAT"),
                        "quantity": float(getattr(pos, "quantity", 0) or 0),
                    }
                    exchange_positions[symbol] = payload
                except Exception as exc:  # noqa: BLE001
                    logger.warning("position %s: %s", symbol, exc)
                try:
                    open_orders.extend(await client.get_open_orders(symbol) or [])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("open orders %s: %s", symbol, exc)
            if "BTCUSDT" in exchange_positions and not status.get("exchange_position"):
                status["exchange_position"] = exchange_positions["BTCUSDT"]
        status["exchange_positions"] = exchange_positions
        status["open_orders"] = open_orders
        # 人工干预状态：运行器检测到网页/面板手动下单后会写进状态文件。
        # 页面据此显示「已暂停自动开仓」并提供人工确认恢复。
        status["external_interventions"] = external_summary(live)
        return web.json_response(status)

    async def api_external_resume(request):
        """人工确认恢复：写入一次性请求，运行器下一轮消费。"""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        symbol = str((body or {}).get("symbol") or "").strip().upper()
        if symbol not in TRADE_SYMBOLS:
            return web.json_response(
                {"ok": False, "error": f"未知标的: {symbol or '(空)'}"}, status=400
            )
        at = request_external_resume(symbol, now=now_ms())
        return web.json_response({"ok": True, "symbol": symbol, "requested_ms": at})

    async def api_trading_flatten_orphan(request):
        # 归属闸门：属于 shadow runner 的仓位，面板不得平掉。
        client0 = review.executor.client
        symbol0 = getattr(client0, "symbol", None) or "BTCUSDT"
        if client0.configured:
            try:
                pos0 = await client0.get_position(symbol0)
                signed0 = float(getattr(pos0, "quantity", 0) or 0) * (
                    1.0 if (getattr(pos0, "side", "FLAT") or "").upper() == "LONG"
                    else -1.0 if (getattr(pos0, "side", "FLAT") or "").upper() == "SHORT"
                    else 0.0)
                blocked = shadow_ledger_claim(symbol0, signed0)
                if blocked:
                    return web.json_response(
                        {"ok": False, "stage": "ownership_gate",
                         "error": "shadow_owned_position", "detail": blocked},
                        status=409,
                    )
            except Exception as exc:  # noqa: BLE001  闸门自身出错不阻断既有流程
                logger.warning("flatten_orphan 归属判定失败: %s", exc)
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
            # tag="smk": 这是功能测试, 不是策略交易。标签让事后能可靠分开,
            # 不靠时间窗口猜测。
            entry = await client.place_limit_chase("LONG", qty, tag="smk")
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
                "SHORT", filled, reduce_only=True, tag="smk"
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

    async def api_testnet_flatten_now(request):
        """平掉当前观测到的 Testnet BTCUSDT 仓位，并强制重新对账。

        平仓后必须对账，否则本地账本会与交易所脱节并永久阻塞开仓。

        归属闸门（2026-10-04）：若该仓位归 shadow runner 策略账本，默认拒绝；
        只有请求体显式带 {"force": true} 才放行——避免面板误点平掉策略仓。
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001  无 body / 非 JSON 都按未强制处理
            body = {}
        force = bool((body or {}).get("force"))
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
            # 归属闸门：属于 shadow runner 的仓位必须显式强制才允许平。
            signed_now = qty if side == "LONG" else (-qty if side == "SHORT" else 0.0)
            blocked = shadow_ledger_claim(getattr(client, "symbol", None) or "BTCUSDT",
                                          signed_now)
            if blocked and not force:
                return web.json_response(
                    {"ok": False, "stage": "ownership_gate",
                     "error": "shadow_owned_position", "detail": blocked,
                     "hint": '确认要由面板强平时，请带 {"force": true} 重发。'},
                    status=409,
                )
            close = await client.place_limit_chase(
                "SHORT" if side == "LONG" else "LONG", qty, reduce_only=True,
                tag="usr",
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

    async def _binance_key_payload(request):
        """读取并校验测试网密钥请求；不返回任何明文凭据。"""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 允许空 body
            body = {}
        api_key = str(body.get("api_key") or "").strip()
        api_secret = str(body.get("api_secret") or "").strip()
        base_url = str(body.get("base_url") or "").strip()
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret required")
        return api_key, api_secret, base_url

    async def api_binance_keys_save(request):
        """仅保存并热切换测试网客户端，不联网、不对账、不下单。"""
        try:
            api_key, api_secret, base_url = await _binance_key_payload(request)
            new_client = BinanceTestnetClient(
                api_key=api_key,
                api_secret=api_secret,
                base_url=base_url or None,
            )
            save_secrets({
                "binance_testnet_api_key": api_key,
                "binance_testnet_api_secret": api_secret,
                **({"binance_testnet_base_url": base_url} if base_url else {}),
            })
            old_client = review.executor.client
            review.executor.client = new_client
            review.executor.manager.client = new_client
            await old_client.close()
            return web.json_response({
                "ok": True,
                "saved": True,
                "key_masked": mask_secret(api_key),
                "base_url": new_client.base_url,
                "note": "测试网凭据已保存到本机；尚未执行通信测试。",
            })
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return web.json_response(
                {"ok": False, "error": f"写入本地凭据文件失败: {exc}"}, status=500
            )
        except Exception as exc:  # noqa: BLE001 - 客户端构造校验等
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def api_binance_keys_test(request):
        """只读通信测试：ping + 账户余额 + 标记价格，不保存、不对账。"""
        client = None
        try:
            try:
                api_key, api_secret, base_url = await _binance_key_payload(request)
            except ValueError:
                secrets = load_secrets()
                api_key = secrets.get("binance_testnet_api_key") or ""
                api_secret = secrets.get("binance_testnet_api_secret") or ""
                base_url = secrets.get("binance_testnet_base_url") or ""
                if not api_key or not api_secret:
                    raise ValueError("尚未保存测试网凭据，请先填写并保存")
            client = BinanceTestnetClient(
                api_key=api_key,
                api_secret=api_secret,
                base_url=base_url or None,
            )
            info = await client.verify()
            return web.json_response({
                "ok": True,
                "tested": True,
                **info,
                "note": "币安测试网通信正常；测试过程未下单、撤单或修改账户设置。",
            })
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"通信测试失败: {exc}"}, status=400
            )
        finally:
            if client is not None:
                await client.close()

    # ------------------------------------------------------------ 主网凭据（只读）
    #
    # 安全约定（2026-10-05 用户要求，务必遵守）：
    #   * 主网凭据只落 runtime/secrets.json（已 gitignore，权限 0600）；
    #   * 本组接口只做「保存 / 删除 / 只读测试」，绝不下单、不重绑测试网客户端；
    #   * 保存主网凭据不会改变运行器模式，主网执行仍由 runtime_mode 硬阻断；
    #   * 任何响应都不得回显 API Key/Secret 明文。
    async def api_mainnet_keys(request):
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        api_secret = (body.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            return web.json_response(
                {"ok": False, "error": "api_key and api_secret required"},
                status=400,
            )
        try:
            save_secrets({
                "binance_mainnet_api_key": api_key,
                "binance_mainnet_api_secret": api_secret,
                "binance_mainnet_base_url": MAINNET_USDM_BASE,
            })
        except OSError as exc:
            return web.json_response(
                {"ok": False, "error": f"写入本地凭据文件失败: {exc}"}, status=500
            )
        return web.json_response({
            "ok": True,
            "saved": True,
            "mainnet_key_masked": mask_secret(api_key),
            "base_url": MAINNET_USDM_BASE,
            "execution_enabled": False,
            "note": "已保存到本机 secrets 文件；未启用主网交易，也未改动运行器。",
        })

    async def api_mainnet_forget(_request):
        """删除本机保存的主网凭据。测试网凭据与运行器不受影响。"""
        save_secrets({
            "binance_mainnet_api_key": None,
            "binance_mainnet_api_secret": None,
            "binance_mainnet_base_url": None,
        })
        return web.json_response({"ok": True, "forgotten": True})

    async def api_mainnet_verify(request):
        """签名只读测试：GET /fapi/v1/time + GET /fapi/v2/account。"""
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 允许空 body，回落到已保存凭据
            body = {}
        secrets = load_secrets()
        api_key = (body.get("api_key") or "").strip() or (
            secrets.get("binance_mainnet_api_key") or ""
        )
        api_secret = (body.get("api_secret") or "").strip() or (
            secrets.get("binance_mainnet_api_secret") or ""
        )
        if not api_key or not api_secret:
            return web.json_response(
                {"ok": False, "error": "尚未保存主网凭据，请先保存再测试"},
                status=400,
            )
        try:
            info = await verify_usdm_credentials_readonly(api_key, api_secret)
        except MainnetReadOnlyError as exc:
            return web.json_response(
                {"ok": False, "error": str(exc), "execution_enabled": False},
                status=400,
            )
        return web.json_response({
            **info,
            "key_masked": mask_secret(api_key),
            "note": "只读预检通过。这不代表可以安全启动真实交易，"
                    "主网执行仍需人工审查与显式启用。",
        })

    async def api_alerts(request):
        """运行器事件告警流（runtime/shadow/alerts.jsonl 尾部，只读）。"""
        try:
            limit = int(request.rel_url.query.get("limit", "50"))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 300))
        path = os.path.join(ROOT, "runtime", "shadow", "alerts.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return web.json_response(
                {"ok": False, "reason": "alerts_file_missing", "alerts": []}
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "alerts": []}
            )
        alerts: list = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                alerts.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        alerts.reverse()  # 最新在前
        return web.json_response({"ok": True, "alerts": alerts, "count": len(alerts)})

    async def api_monitor(_request):
        """运行监控附属数据：行情流健康 / 运行器锁 / 心跳 / 巡检日志尾部（只读）。"""
        def read_json(name: str):
            try:
                with open(
                    os.path.join(ROOT, "runtime", "shadow", name), encoding="utf-8"
                ) as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                return None

        monitor_tail: list = []
        try:
            with open(
                os.path.join(ROOT, "runtime", "shadow", "monitor.log"),
                encoding="utf-8",
            ) as fh:
                monitor_tail = fh.read().splitlines()[-12:]
        except Exception:  # noqa: BLE001
            pass
        return web.json_response({
            "ok": True,
            "stream_health": read_json("market_stream_health.json"),
            "lock": read_json("trader.lock"),
            "heartbeat": read_json("runner_heartbeat.json"),
            "monitor_tail": monitor_tail,
        })

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

    async def api_strategy_reading(_request):
        """策略此刻的读数快照 —— 直接取自运行器落盘的 latest_reading*.json。

        这是核对信号的唯一正确入口: 它记录的是 bot 真正使用的那条行情序列
        (市场、K 线地址、OHLC、K/D、ATR 全都在), 而不是某个图表显示的东西。
        2026-10-02 的市场错位之所以难查, 正是因为当时没有这个快照。
        """
        paths = [
            os.path.join(ROOT, "runtime", "shadow", "latest_reading.json"),
            os.path.join(ROOT, "runtime", "shadow", "latest_reading_5m.json"),
            os.path.join(ROOT, "runtime", "shadow", "latest_reading_eth15.json"),
            os.path.join(ROOT, "runtime", "shadow", "latest_reading_eth5.json"),
        ]
        heartbeat_path = os.path.join(
            ROOT, "runtime", "shadow", "runner_heartbeat.json"
        )
        heartbeat = None
        try:
            with open(heartbeat_path, encoding="utf-8") as fh:
                heartbeat = json.load(fh)
        except Exception:
            pass
        readings = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    readings.append(_with_reading_series(json.load(fh)))
            except Exception:
                continue
        if not readings:
            return web.json_response({
                "available": False,
                "reason": "runner_not_written_yet",
                "runner": heartbeat,
                "readings": [],
            })
        rec = dict(readings[0])
        rec["available"] = True
        rec["runner"] = heartbeat
        rec["readings"] = readings
        return web.json_response(rec)

    async def api_strategies_active(_request):
        """当前运行的策略定义 + 实时运行态 (数组结构, 为多策略并列预留).

        定义取自 config/strategies/*.json; 运行态取自
        runtime/shadow/deployed_state.json。仅 enabled 的策略按 runtime_key
        挂自己的虚拟仓，不再两张卡共用整份顶层状态。
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
                view = runtime_view(
                    live, d.get("strategy_id") or "",
                    runtime_key=d.get("runtime_key"),
                ) if d.get("enabled") else None
                if d.get("enabled") and view is None and live:
                    view = live
                out.append({**d, "runtime": view})
            out.sort(key=lambda row: (
                str(row.get("symbol") or ""),
                str(row.get("timeframe") or ""),
            ))
        except Exception:
            pass
        return web.json_response({"strategies": out})

    async def api_binance_orders(request):
        """币安委托：未传起点时交易所仅返回最近七天，绝非全部历史。"""
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing", "orders": []}
            )
        try:
            limit = max(1, min(200, int(request.query.get("limit", 200))))
        except Exception:
            limit = 200
        try:
            orders = await _rows_for_symbols(client, "all_orders", limit)
            # 来源标签: 策略单 / 网页手动 / 功能测试 / 面板手动 / 未判定。
            # 交易所记录不删不改, 只加可核验的标签 —— 见 review/order_sources.py。
            orders = annotate_orders(
                orders, strategy_order_ids=load_strategy_order_ids()
            )
            # 记录起点：用户要求历史清零，交易所记录无法删除，只能过滤显示。
            start = stats_start_ms()
            if start:
                orders = [
                    o for o in orders
                    if int(o.get("updateTime") or o.get("time") or 0) >= start
                ]
            counts = summarize(orders)
            return web.json_response({
                "connected": True,
                "orders": orders,
                "scope": "recent_7d", "limit": limit,
                "truncated": len(orders) >= limit,
                "record_start": record_start_label(),
                "source_counts": counts,
                "source_labels": SOURCE_LABELS,
            })
        except Exception as exc:
            return web.json_response({
                "connected": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "orders": [],
            })

    async def api_binance_trades(request):
        """币安逐笔成交：默认最近七天；一笔委托可拆为多笔成交。"""
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing", "trades": []}
            )
        try:
            limit = max(1, min(200, int(request.query.get("limit", 200))))
        except Exception:
            limit = 200
        try:
            trades = await _rows_for_symbols(client, "user_trades", limit)
            # 逐笔成交沿用其所属委托的来源, 避免同一笔单出现两个口径。
            try:
                parents = annotate_orders(
                    await _rows_for_symbols(client, "all_orders", limit),
                    strategy_order_ids=load_strategy_order_ids(),
                )
            except Exception:  # noqa: BLE001
                parents = []
            trades = annotate_trades(
                trades, parents, strategy_order_ids=load_strategy_order_ids()
            )
            # 记录起点：与委托同一起点，避免「委托已清零、成交还在」。
            start = stats_start_ms()
            if start:
                trades = [
                    t for t in trades if int(t.get("time") or 0) >= start
                ]
            counts = summarize(trades)
            return web.json_response({
                "connected": True,
                "trades": trades,
                "scope": "recent_7d", "limit": limit,
                "truncated": len(trades) >= limit,
                "record_start": record_start_label(),
                "source_counts": counts,
                "source_labels": SOURCE_LABELS,
            })
        except Exception as exc:
            return web.json_response({
                "connected": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "trades": [],
            })

    async def api_account_summary(_request):
        """账户汇总: 余额 + 已实现/未实现盈亏 + 手续费 + 胜率与盈亏比.

        口径（2026-10-04 审计后修正）: 顶层是**自记录起点起的完整持仓周期**。
        每一笔 = 一次从空仓到再次空仓的完整交易（开仓/加层/减仓/反手/平仓
        配对），净额 = 已实现 − 手续费 + 资金费。不再是「每标的最近 200 笔
        成交腿」——那会把分批平仓算成多笔交易、并漏掉窗口外的开仓手续费。

        覆盖度由 coverage 字段给出：数据起点、是否被截断、以及有没有被窗口
        切成两半的持仓（orphan_closes）。
        """
        client = review.executor.client
        if not client.configured:
            return web.json_response(
                {"connected": False, "reason": "binance_keys_missing"}
            )
        try:
            bal = await client.get_balance()
            pos = await client.get_position()
            upnl = float(getattr(bal, "total_unrealized_pnl", 0) or 0)
            if not upnl:
                upnl = float(getattr(pos, "unrealized_pnl", 0) or 0)
            # 完整持仓周期账本：分页取全部成交（不再用每标的 200 笔上限，
            # 那会把开仓腿截断在窗口之外、漏掉开仓手续费），再按开仓/加层/
            # 减仓/反手/平仓配成完整交易，最后按时间把资金费归属到周期。
            # 因此下面每一个「笔」= 一个完整持仓周期，而不是一笔成交腿。
            start = stats_start_ms()
            now_ms = int(time.time() * 1000)
            cached = _LEDGER_CACHE.get("data")
            if cached is None or now_ms - int(_LEDGER_CACHE.get("at_ms") or 0) > LEDGER_TTL_MS:
                # 回看 7 天：足以接上跨记录起点的持仓，又不会把窗口拉太长。
                try:
                    from review.order_sources import load_strategy_order_ids
                    strat_orders = set(load_strategy_order_ids())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("策略委托台账读取失败，全部记为未识别: %s", exc)
                    strat_orders = set()
                cached = await cycle_pnl.build_ledger(
                    client, TRADE_SYMBOLS, start, context_days=7,
                    strategy_orders=strat_orders,
                )
                _LEDGER_CACHE["data"] = cached
                _LEDGER_CACHE["at_ms"] = now_ms
            ledger = cached
            if ledger["errors"] and not ledger["per_symbol"]:
                first = next(iter(ledger["errors"].values()))
                return web.json_response(
                    {"connected": False, "reason": f"成交查询失败: {first}"}
                )

            scoped = dict(ledger["total"])
            every = dict(scoped)

            pos_by_symbol: Dict[str, Any] = {}
            for sym in TRADE_SYMBOLS:
                try:
                    p = await client.get_position(sym)
                    pos_by_symbol[sym] = {
                        "symbol": sym,
                        "side": str(getattr(p, "side", "FLAT") or "FLAT"),
                        "quantity": float(getattr(p, "quantity", 0) or 0),
                        "entry_price": float(getattr(p, "entry_price", 0) or 0),
                        "unrealized_pnl": float(
                            getattr(p, "unrealized_pnl", 0) or 0
                        ),
                        "notional": float(getattr(p, "notional", 0) or 0),
                        "margin": float(getattr(p, "margin", 0) or 0),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.warning("summary position %s: %s", sym, exc)
            by_symbol: Dict[str, Any] = {}
            extra = set(ledger["per_symbol"]) - set(TRADE_SYMBOLS)
            for sym in list(TRADE_SYMBOLS) + sorted(s for s in extra if s):
                item = dict(ledger["per_symbol"].get(sym) or {})
                if not item:
                    item = dict(cycle_pnl.summarize([]))
                item["symbol"] = sym
                item["unrealized_pnl"] = float(
                    (pos_by_symbol.get(sym) or {}).get("unrealized_pnl", 0) or 0
                )
                by_symbol[sym] = item
            # 本地权益回撤与原因记录同样从记录起点重新累计。
            quality_ctx = _quality_context(start)
            # 单笔净边际：分母用开仓名义金额（不依赖复利），口径与分标的完全一致。
            edge_bps = scoped.get("unit_edge_bps")
            slippage = _slippage_estimate(pos_by_symbol, start)
            drawdown = {
                # 账户权益口径：运行器落盘的权益曲线峰值回撤。
                "account_recorded": quality_ctx["recorded_max_drawdown"],
                "account_basis": "runtime/shadow/deployed_trades.csv 权益字段（运行记录口径）",
                # 按标的分组：各自已实现净额曲线回撤，绝不混加。
                "by_symbol": {
                    sym: (by_symbol.get(sym) or {}).get("realized_max_drawdown")
                    for sym in by_symbol
                },
                "by_symbol_basis": "各标的按完整持仓周期净额（已实现 − 手续费 + 资金费）在平仓时刻推进的峰值回撤，USDT",
            }
            costs = {
                "maker_fee": float(scoped.get("maker_fee") or 0),
                "taker_fee": float(scoped.get("taker_fee") or 0),
                "total_fee": float(scoped.get("commission") or 0),
                "funding_fee": float(scoped.get("funding_fee") or 0),
                "funding_orphan": float(scoped.get("funding_orphan") or 0),
                "funding_basis": (
                    "交易所 income 明细 incomeType=FUNDING_FEE，按时间归属到当时"
                    "持仓的完整周期；归属不上的记为 funding_orphan 单列"
                ),
                "slippage_est": slippage["slippage_est"],
                "slippage_matched": slippage["slippage_matched"],
                "slippage_candidates": slippage["slippage_candidates"],
                "slippage_basis": slippage["slippage_basis"],
            }
            reasons = list(quality_ctx["recent_reasons"])
            if quality_ctx["five_minute_disabled"]:
                reasons.insert(0, {
                    "text": "当前运行器仅启用 BTC/ETH 15m；5m 已停用，避免趋势中无正常出口导致灾难止损",
                    "count": 1,
                })
            actions = [
                "继续固定 15m-only 测试网窗口，不在盈利后临时改参数或扩大仓位",
                "累计至少 4 周或 30 个完整平仓样本，再评估单笔净边际和跨 BTC/ETH 一致性",
                "单笔口径已改为完整持仓周期（开仓/加层/减仓/反手/平仓配对 + 资金费归属），继续累计完整平仓样本再评估边际",
            ]
            if quality_ctx["halted"]:
                actions.insert(0, "运行器处于熔断状态：先人工核对交易所净仓与本地账本，再决定是否恢复")
            return web.json_response({
                "connected": True,
                # 记录起点：stats_start 保留为日期，record_start 是精确到分钟的文案。
                "stats_start": record_start_label()[:10],
                "record_start": record_start_label(),
                "record_start_ms": start,
                "record_start_note": (
                    "历史清零后重新记录：委托、成交、成本、回撤与明细均自该时刻起"
                    "统计；持仓、余额、挂单为实时状态，不受影响。"
                ),
                # 口径：自记录起点起的**完整持仓周期**，不再是「最近 7 天
                # 每标的 200 笔成交腿」。coverage 说明数据从哪开始、有没有被
                # 截断、有没有被窗口切成两半的持仓。
                "scope": "record_start_full_cycles",
                "history_limit": None,
                "truncated": bool((scoped.get("coverage") or {}).get("truncated")),
                "coverage": scoped.get("coverage"),
                "funding_rows": scoped.get("funding_rows"),
                "funding_note": scoped.get("funding_note"),
                "wallet_balance": float(
                    getattr(bal, "total_wallet_balance", 0) or 0
                ),
                "available_balance": float(
                    getattr(bal, "available_balance", 0) or 0
                ),
                "unrealized_pnl": upnl,
                # 起算日口径（用户指定的「赚了多少」）
                **scoped,
                # 分标的（BTC / ETH 各自统计，不混加）
                "by_symbol": by_symbol,
                "positions_by_symbol": pos_by_symbol,
                # 与最近七天、最多200笔的表格对齐，不提供假「全历史」。
                "available_history": every,
                "position": {
                    "side": str(getattr(pos, "side", "FLAT") or "FLAT"),
                    "quantity": float(getattr(pos, "quantity", 0) or 0),
                    "entry_price": float(getattr(pos, "entry_price", 0) or 0),
                },
                "quality": {
                    "realized_net_pnl": float(scoped["net_pnl"]),
                    "unrealized_pnl": float(upnl),
                    "unit_edge_bps": edge_bps,
                    "unit_edge_basis": "开仓名义金额口径：净额（已实现 − 手续费 + 资金费）÷ 开仓名义 × 10000，按完整持仓周期统计（不依赖复利）",
                    "open_notional": float(scoped.get("open_notional") or 0),
                    "recorded_max_drawdown": quality_ctx["recorded_max_drawdown"],
                    "drawdown": drawdown,
                    "drawdown_basis": drawdown["account_basis"],
                    # 胜率与盈亏比必须一起看，不能只报胜率。
                    "win_rate": float(scoped.get("win_rate") or 0),
                    "profit_factor": scoped.get("profit_factor"),
                    "payoff_ratio": scoped.get("payoff_ratio"),
                    "wins": scoped.get("wins"),
                    "losses": scoped.get("losses"),
                    "avg_win": scoped.get("avg_win"),
                    "avg_loss": scoped.get("avg_loss"),
                    # 交易成本单列：maker / taker / 资金费 / 滑点估算。
                    "costs": costs,
                    "window": record_start_label(),
                    "reasons": reasons,
                    "next_actions": actions,
                    "context": quality_ctx,
                },
            })
        except Exception as exc:
            return web.json_response({
                "connected": False, "reason": f"{type(exc).__name__}: {exc}"
            })

    async def api_chart(request):
        """图片显示确认模块：K 线 + KDJ + MACD + B/S 信号（只读，仅测试网）。

        行情地址由 config.market_endpoints 从账户地址反推，与运行器同一条序列；
        KDJ/MACD 直接复用 shadow/indicators.py 的同一份函数，不另立口径。
        """
        symbol = (request.query.get("symbol") or "BTCUSDT").upper()
        interval = (request.query.get("interval") or "15m").lower()
        try:
            bars = int(request.query.get("bars") or 200)
        except Exception:
            bars = 200
        try:
            from review import chart_data

            payload = await chart_data.get_chart(symbol, interval, bars)
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response({
                "ok": False,
                "module": "图片显示确认模块",
                "symbol": symbol,
                "interval": interval,
                "reason": f"{type(exc).__name__}: {exc}",
            }, status=502)

    async def api_equity_curve(_request):
        """只读：运行器记录口径的权益曲线。

        取自 runtime/shadow/deployed_trades.csv 的「权益」列，与
        quality.recorded_max_drawdown 同一口径。不修改任何状态、不联网。
        """
        path = os.path.join(ROOT, "runtime", "shadow", "deployed_trades.csv")
        points: list = []
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        value = float(str(row.get("权益") or "").replace(",", ""))
                    except (TypeError, ValueError):
                        continue
                    if value <= 0:
                        continue
                    points.append({
                        "t": _trade_row_ms(row),
                        "equity": value,
                        "action": str(row.get("动作") or ""),
                    })
        except FileNotFoundError:
            points = []
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            )
        # 同一根 K 线会有 BTC/ETH 两行；时间戳还可能乱序，
        # 直接按行画曲线会来回折返，所以先按时间排序再按时刻去重。
        points.sort(key=lambda item: item["t"])
        dedup: list = []
        for item in points:
            if dedup and dedup[-1]["t"] == item["t"]:
                dedup[-1] = item
            else:
                dedup.append(item)
        points = dedup
        peak = 0.0
        max_dd = 0.0
        for item in points:
            peak = max(peak, item["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - item["equity"]) / peak)
        return web.json_response({
            "ok": True,
            "points": points,
            "count": len(points),
            "first": points[0]["equity"] if points else None,
            "last": points[-1]["equity"] if points else None,
            "peak": peak or None,
            "max_drawdown": max_dd,
            "basis": "deployed_trades.csv 权益字段（运行记录口径）",
        })

    # routes
    app.router.add_get("/", index)
    app.router.add_get(
        "/{name:app\\.js|chart\\.js|styles\\.css|index\\.html}", static_asset
    )
    app.router.add_get("/api/live", api_live)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/journal", api_journal_list)
    app.router.add_post("/api/journal", api_journal_add)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/strategies", api_strategies)
    app.router.add_get("/api/strategies/active", api_strategies_active)
    app.router.add_get("/api/strategy/reading", api_strategy_reading)
    app.router.add_get("/api/binance/orders", api_binance_orders)
    app.router.add_get("/api/binance/trades", api_binance_trades)
    app.router.add_get("/api/account/summary", api_account_summary)
    app.router.add_get("/api/equity/curve", api_equity_curve)
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
    app.router.add_post("/api/external/resume", api_external_resume)
    app.router.add_get("/api/trading/history", api_trading_history)
    app.router.add_post("/api/paper/test-order", api_paper_test_order)
    app.router.add_post("/api/paper/test-close", api_paper_test_close)
    app.router.add_post("/api/trading/verify", api_trading_verify)
    app.router.add_post("/api/trading/keys", api_binance_keys)
    app.router.add_post("/api/trading/keys/save", api_binance_keys_save)
    app.router.add_post("/api/trading/keys/test", api_binance_keys_test)
    app.router.add_post("/api/mainnet/keys", api_mainnet_keys)
    app.router.add_post("/api/mainnet/forget", api_mainnet_forget)
    app.router.add_post("/api/mainnet/verify", api_mainnet_verify)
    app.router.add_post("/api/predict_fun/key", api_predict_fun_key)
    app.router.add_get("/api/review/convergence", api_convergence)
    app.router.add_post("/api/scheduler/run", api_scheduler_run)
    app.router.add_get("/api/daily_summaries", api_daily_summaries)
    app.router.add_get("/api/chart", api_chart)
    app.router.add_get("/api/alerts", api_alerts)
    app.router.add_get("/api/monitor", api_monitor)
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
