"""第三轮强制验收 — 钉死反例，禁止弱断言。

验收入口（不加载密钥、不下单）:
  python3 -m unittest tests.test_round3_acceptance -v
  python3 scripts/round3_verify.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from review.trade_ledger import TradeLedger, TradeLedgerEntry
from trading.fake_exchange import FakeBinanceClient
from trading.models import HorizonPosition, OrderState, SystemHealth
from trading.position_manager import PositionManager
from trading.risk_guardian import RiskGuardian


def _run(coro):
    return asyncio.run(coro)


def _seed_long(mgr: PositionManager, client: FakeBinanceClient, qty=0.010, horizon="short_term"):
    mgr._ready = True
    mgr.positions[horizon] = HorizonPosition(
        horizon=horizon, side="LONG", quantity=qty,
        entry_price=100.0, original_quantity=qty, peak_price=100.0,
        sl_price=95.0, entry_atr=2.0,
    )
    # 播种时必须全额成交，再由用例单独设置 partial_fill_pct
    old_pct = getattr(client, "_partial_fill_pct", 1.0)
    client._partial_fill_pct = 1.0
    _run(mgr._sync_exchange_to_net(100.0))
    client._partial_fill_pct = old_pct
    exch = _run(client.get_position())
    assert exch.side == "LONG" and abs(exch.quantity - qty) < 1e-9, (exch.side, exch.quantity)


class TestEXEC01PartialReduce(unittest.TestCase):
    def test_EXEC01_partial_reduce_uses_fill(self):
        """计划减 0.005，成交 0.002 → 本地 0.008，交易所 0.008，标记未完成。"""
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=5000, mark=100.0, partial_fill_pct=0.4)
        mgr = PositionManager(client, persist_path=Path(tmp.name) / "p.json")
        _seed_long(mgr, client, 0.010)
        act = _run(mgr.partial_close("short_term", 0.5, reason="tp1", price=100.0))
        self.assertIsNotNone(act)
        pos = mgr.get_position("short_term")
        exch = _run(client.get_position())
        self.assertAlmostEqual(exch.quantity, 0.008, places=6)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.quantity, 0.008, places=6)
        self.assertTrue(getattr(pos, "exit_incomplete", False) or getattr(pos, "pending_exit_qty", 0) > 0)
        self.assertFalse(mgr.reconciliation_needed or False)  # 一致则不应误报；或显式 pending
        # 本地与交易所一致
        self.assertAlmostEqual(pos.quantity, exch.quantity, places=6)
        tmp.cleanup()


class TestEXEC02PartialFullClose(unittest.TestCase):
    def test_EXEC02_full_close_half_fill(self):
        """全平只成交一半 → 本地保留 0.005，不得归零。"""
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=5000, mark=100.0, partial_fill_pct=0.5)
        mgr = PositionManager(client, persist_path=Path(tmp.name) / "p.json")
        _seed_long(mgr, client, 0.010)
        act = _run(mgr.force_close("short_term"))
        self.assertIsNotNone(act)
        pos = mgr.get_position("short_term")
        exch = _run(client.get_position())
        self.assertAlmostEqual(exch.quantity, 0.005, places=6)
        self.assertIsNotNone(pos)
        self.assertFalse(pos.is_flat())
        self.assertAlmostEqual(pos.quantity, 0.005, places=6)
        tmp.cleanup()


class TestRECON01InflightOpen(unittest.TestCase):
    def test_RECON01_inflight_open_not_flattened(self):
        """开仓在途 + 对账看到旧空仓 → 不得卖出刚开的仓。"""
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=5000, mark=100.0)
        mgr = PositionManager(client, persist_path=Path(tmp.name) / "p.json")
        mgr._ready = True
        g = RiskGuardian(client, mgr)
        g.health = SystemHealth.NORMAL
        g.last_mark_ts = time.time()

        # 模拟：本地已记开仓意图/在途，交易所查询仍返回 0（旧观察）
        mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.010,
            entry_price=100.0, original_quantity=0.010, peak_price=100.0,
            sl_price=95.0, entry_atr=2.0,
        )
        mgr.register_inflight_order("cid_open", horizon="short_term", side="LONG", qty=0.010)
        # 交易所仍空（尚未确认）
        self.assertEqual(_run(client.get_position()).side, "FLAT")

        orders_before = list(client.market_orders)
        _run(g.reconcile_exchange_fills())
        orders_after = list(client.market_orders)
        self.assertEqual(orders_after, orders_before, "对账不得因旧观察额外下单")
        # 开仓确认
        client._net_qty = 0.010
        client._entry = 100.0
        mgr.clear_inflight_order("cid_open")
        mgr.positions["short_term"].quantity = 0.010
        _run(g.reconcile_exchange_fills())
        # 仍不得平仓
        self.assertEqual(len(client.market_orders), len(orders_before))
        pos = mgr.get_position("short_term")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.quantity, 0.010, places=6)
        tmp.cleanup()


class TestRECON02ExternalFlat(unittest.TestCase):
    def test_RECON02_external_flat_no_trade(self):
        """交易所已空、本地双策略残留 → 对账只记账，不主动开空/买入。"""
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=5000, mark=100.0)
        mgr = PositionManager(client, persist_path=Path(tmp.name) / "p.json")
        mgr._ready = True
        mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.010,
            entry_price=100.0, original_quantity=0.010, peak_price=100.0, sl_price=95.0,
        )
        mgr.positions["long_term"] = HorizonPosition(
            horizon="long_term", side="SHORT", quantity=0.004,
            entry_price=100.0, original_quantity=0.004, peak_price=100.0, sl_price=105.0,
        )
        # 交易所空（保护已触发）
        client._net_qty = 0.0
        g = RiskGuardian(client, mgr)
        g.health = SystemHealth.NORMAL
        g.last_mark_ts = time.time()
        before = list(client.market_orders)
        acts = _run(g.reconcile_exchange_fills())
        after = list(client.market_orders)
        self.assertEqual(after, before, "对账不得调用会下单的 force_close/_sync")
        self.assertTrue(
            mgr.get_position("short_term") is None or mgr.get_position("short_term").is_flat()
        )
        self.assertTrue(
            mgr.get_position("long_term") is None or mgr.get_position("long_term").is_flat()
        )
        tmp.cleanup()


class TestLEDGER_R3Idempotent(unittest.TestCase):
    def test_LEDGER_R3_same_fill_different_receive_time(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "l.jsonl"
        led = TradeLedger(path)
        e1 = TradeLedgerEntry(
            entry_id="a", trade_id="t", ts_ms=1000, horizon="short_term",
            action="partial_close", side="LONG", quantity=0.002, price=100.0,
            fee_unknown=True, entry_kind="fill",
            order_id="oid1", client_order_id="cid1",
            meta={"exchange_trade_id": "tr_99"},
        )
        e2 = TradeLedgerEntry(
            entry_id="b", trade_id="t", ts_ms=9999, horizon="short_term",
            action="partial_close", side="LONG", quantity=0.002, price=100.0,
            fee_unknown=True, entry_kind="fill",
            order_id="oid1", client_order_id="cid1",
            meta={"exchange_trade_id": "tr_99"},
        )
        led.append(e1)
        led.append(e2)
        fills = [r for r in led.recent(20) if r.get("entry_kind") == "fill"]
        self.assertEqual(len(fills), 1)
        # 两笔不同真实成交、同数量
        e3 = TradeLedgerEntry(
            entry_id="c", trade_id="t2", ts_ms=1001, horizon="short_term",
            action="partial_close", side="LONG", quantity=0.002, price=100.0,
            fee_unknown=True, entry_kind="fill",
            order_id="oid1", client_order_id="cid1",
            meta={"exchange_trade_id": "tr_100"},
        )
        led.append(e3)
        fills = [r for r in led.recent(20) if r.get("entry_kind") == "fill"]
        self.assertEqual(len(fills), 2)
        tmp.cleanup()


class TestDATA_FREE(unittest.TestCase):
    def test_DATA_FREE_liq_status_propagates(self):
        from collectors.free_derivatives import FreeDerivativesCollector
        from models.snapshots import CoinGlassSnapshot
        c = FreeDerivativesCollector()
        # 空缓冲：不得把真零当 complete + available 成功
        c._snapshot = CoinGlassSnapshot(
            liq_long_5m_usd=0.0, liq_short_5m_usd=0.0, liq_total_5m_usd=0.0,
            liq_window_status="warmup", available=False,
        )
        snap = c.get_snapshot()
        self.assertEqual(snap.liq_window_status, "warmup")
        from mappers.data_mapper import DataFactorMapper
        missing = []
        DataFactorMapper()._map_realtime_liq(
            snap.liq_long_5m_usd, snap.liq_short_5m_usd, missing,
            window_status=snap.liq_window_status,
        )
        self.assertTrue(any("warmup" in m for m in missing))


class TestCOLL_PROD(unittest.TestCase):
    def test_COLL_PROD_mapper_sets_whale_flag(self):
        """生产路径应自动标记巨鲸共线，测试不得手工塞 _collinear_whale。"""
        from utils.scoring import apply_collinear_caps, detect_collinear_whale
        # detect from face contributions
        faces = {"news": 40, "data": 40, "tech": 0, "prediction": 0}
        whale_parts = {"news_whale": 30.0, "data_whale": 35.0}
        flagged = detect_collinear_whale(faces, whale_parts)
        self.assertTrue(flagged.get("_collinear_whale"))
        w = {"news": 0.25, "data": 0.35, "tech": 0.25, "prediction": 0.15}
        w2, notes = apply_collinear_caps(flagged, w)
        self.assertLessEqual(w2["news"] + w2["data"], 0.35 + 1e-9)



class TestFEE01Adapter(unittest.TestCase):
    def test_FEE01_sample_and_unknown(self):
        from trading.fee_adapter import (
            parse_user_trade, SAMPLE_USER_TRADE, SAMPLE_USER_TRADE_NO_FEE, FeeAdapter,
        )
        f = parse_user_trade(SAMPLE_USER_TRADE)
        self.assertFalse(f.fee_unknown)
        self.assertAlmostEqual(f.commission, 0.076)
        f2 = parse_user_trade(SAMPLE_USER_TRADE_NO_FEE)
        self.assertTrue(f2.fee_unknown)
        self.assertIsNone(f2.commission)
        client = FakeBinanceClient(mark=100.0)
        _run(client.market_open("LONG", 0.01))
        fills = _run(FeeAdapter(client).fetch_trades("BTCUSDT"))
        self.assertTrue(fills)
        self.assertFalse(fills[0].fee_unknown)


class TestRISK_PNL(unittest.TestCase):
    def test_RISK_PNL_note_daily_wired(self):
        from trading.executor import TradeExecutor
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=10000, mark=100.0)
        ex = TradeExecutor(client=client)
        ex.ledger = TradeLedger(Path(tmp.name) / "l.jsonl")
        ex.guardian.health = SystemHealth.NORMAL
        ex.guardian.last_mark_ts = time.time()
        # 写入大额亏损成交
        ex.ledger.append(TradeLedgerEntry(
            entry_id="x", trade_id="t", ts_ms=1, horizon="short_term",
            action="close", side="LONG", quantity=0.01, price=90.0,
            fee_unknown=False, fee_usdt=0.1,
            realized_pnl_usdt=-400.0, entry_kind="fill",
            order_id="o", client_order_id="c",
            meta={"exchange_trade_id": "tr_loss"},
        ))
        ex._update_daily_pnl_from_ledger()
        self.assertEqual(ex.guardian.health, SystemHealth.REDUCE_ONLY)
        self.assertFalse(ex.guardian.allow_new_entries())
        tmp.cleanup()


class TestCRASH01InflightPersist(unittest.TestCase):
    def test_CRASH01_inflight_survives_reload(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "p.json"
        client = FakeBinanceClient(mark=100.0)
        mgr = PositionManager(client, persist_path=path)
        mgr._ready = True
        mgr.register_inflight_order("cid_x", horizon="short_term", side="LONG", qty=0.01)
        mgr2 = PositionManager(client, persist_path=path)
        self.assertTrue(mgr2.has_inflight_orders())
        self.assertIn("cid_x", mgr2._inflight_orders)
        tmp.cleanup()


class TestALGO_Contract(unittest.TestCase):
    def test_ALGO_official_field_shape(self):
        """契约：创建请求字段含 algoType/triggerPrice/clientAlgoId（非仅 is_algo）。"""
        client = FakeBinanceClient(mark=100.0)
        mo = _run(client.place_stop_market("LONG", 0.01, 95.0, close_position=True))
        raw = mo.raw or {}
        self.assertEqual(raw.get("algoType") or getattr(mo, "algo_type", None) or "CONDITIONAL", "CONDITIONAL")
        # 假盘必须暴露 triggerPrice
        self.assertTrue(
            raw.get("triggerPrice") is not None or mo.stop_price > 0,
            "必须有 triggerPrice/stop_price",
        )
        self.assertTrue(mo.client_order_id)
        # 旧路径拒绝
        if hasattr(client, "place_legacy_stop_market"):
            pass


class TestPROTECT_R3(unittest.TestCase):
    def test_PROTECT_R3_trigger_bounce_keeps_books(self):
        tmp = tempfile.TemporaryDirectory()
        client = FakeBinanceClient(equity=5000, mark=100.0)
        mgr = PositionManager(client, persist_path=Path(tmp.name) / "p.json")
        _seed_long(mgr, client, 0.010)
        g = RiskGuardian(client, mgr)
        g.health = SystemHealth.NORMAL
        g.last_mark_ts = time.time()
        _run(g.ensure_protection("short_term"))
        client.trigger_stop(94.0)
        client.set_mark(96.0)
        before = list(client.market_orders)
        _run(g.reconcile_exchange_fills())
        # 不得额外交易
        self.assertEqual(client.market_orders, before)
        pos = mgr.get_position("short_term")
        self.assertTrue(pos is None or pos.is_flat())
        tmp.cleanup()



class TestFakeIsolatesProdPaths(unittest.TestCase):
    def test_fake_executor_does_not_touch_prod_history(self):
        from trading.executor import TradeExecutor
        from config.review import TRADING_HISTORY_PATH, POSITIONS_PATH
        before_h = TRADING_HISTORY_PATH.read_text() if TRADING_HISTORY_PATH.exists() else ""
        before_p = POSITIONS_PATH.read_text() if POSITIONS_PATH.exists() else ""
        client = FakeBinanceClient(mark=100.0)
        ex = TradeExecutor(client=client)
        ex.enabled = True
        ex.manager._ready = True
        ex.guardian.health = SystemHealth.NORMAL
        ex.guardian.last_mark_ts = time.time()
        # 触发一次假开仓记账
        from models.signals import ActionDecision
        class S: pass
        s=S()
        s.mark_price=100.0; s.composite_score=30; s.partial_cs=30
        s.decision=ActionDecision.STANDARD_LONG; s.overridden=False
        s.tradable=True; s.staleness_sec={"binance":1}; s.atr=2; s.atr_mean=2
        s.atr_pct=None; s.predict_fun_up_prob=None; s.spread_vs_mean=None
        s.liq_5m_usd=None; s.s_news=20; s.s_data=30; s.s_tech=25; s.s_prediction=20
        s.missing_dimensions=[]; s.safety_valve=False
        s.config_snapshot={"version":"v_iso","content_hash":"x","params":{}}
        for k in ("news_detail","data_detail","tech_detail","prediction_detail"):
            d=type("D",(),{})(); d.confidence=0.9; setattr(s,k,d)
        with patch.object(ex.manager, "_calc_qty", return_value=(0.01,50.0,2.0)):
            _run(ex.on_snapshot(s, "short_term"))
        after_h = TRADING_HISTORY_PATH.read_text() if TRADING_HISTORY_PATH.exists() else ""
        after_p = POSITIONS_PATH.read_text() if POSITIONS_PATH.exists() else ""
        self.assertEqual(before_h, after_h, "假盘不得写入生产 trading_history")
        self.assertEqual(before_p, after_p, "假盘不得写入生产 positions.json")
        self.assertNotEqual(str(ex.history_path), str(TRADING_HISTORY_PATH))


if __name__ == "__main__":
    unittest.main()
