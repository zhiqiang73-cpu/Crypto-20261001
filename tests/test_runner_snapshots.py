"""运行器读数/心跳快照测试：不连接交易所、不提交委托。"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest

import numpy as np

import shadow.deploy as deploy


class TestSignalReadingSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_reading = deploy.READING
        self.old_heartbeat = deploy.HEARTBEAT
        deploy.READING = os.path.join(self.tmp.name, "reading.json")
        deploy.HEARTBEAT = os.path.join(self.tmp.name, "heartbeat.json")

    def tearDown(self):
        deploy.READING = self.old_reading
        deploy.HEARTBEAT = self.old_heartbeat
        self.tmp.cleanup()

    def test_numpy_cross_values_become_json_boolean_and_file_is_current(self):
        # 上一根 K<D，本根 K>D —— 金叉；NumPy 标量是历史上导致静默写入失败的根因。
        ts = np.array([1000, 2000], dtype=np.int64)
        o = h = l = c = np.array([100.0, 101.0])
        k = np.array([49.0, 51.0])
        d = np.array([50.0, 50.0])
        atr = np.array([10.0, 10.0])
        up = np.array([102.0, 103.0])
        lb = np.array([98.0, 99.0])
        deploy.save_signal_reading(
            {"missed_bars": 3, "missed_signals": 1}, i=1, ts=ts, o=o, h=h,
            l=l, c=c, k=k, d=d, atr_al=atr, up=up, lb=lb, now=3000,
            execute=False, pos_side="FLAT",
        )
        with open(deploy.READING, encoding="utf-8") as fh:
            out = json.load(fh)
        self.assertEqual(out["bar_ms"], 2000)
        self.assertIs(out["signal_long"], True)
        self.assertIs(out["signal_short"], False)
        self.assertIs(out["gold"], True)
        self.assertEqual(out["series"]["k"], [49.0, 51.0])
        self.assertEqual(out["missed_bars"], 3)
        self.assertEqual(out["run_mode_at_snapshot"], "observation_only")
        self.assertEqual(out["position"], "空仓")

    def test_nan_values_are_json_null_not_invalid_nan(self):
        ts = np.array([1000, 2000], dtype=np.int64)
        values = np.array([100.0, 101.0])
        deploy.save_signal_reading(
            {}, i=1, ts=ts, o=values, h=values, l=values, c=values,
            k=np.array([49.0, 51.0]), d=np.array([50.0, 50.0]),
            atr_al=np.array([np.nan, np.nan]), up=np.array([np.nan, np.nan]),
            lb=np.array([np.nan, np.nan]), now=3000, execute=True,
            pos_side="LONG",
        )
        with open(deploy.READING, encoding="utf-8") as fh:
            out = json.load(fh)
        self.assertIsNone(out["ATR_1H"])
        self.assertIsNone(out["mult"])
        self.assertEqual(out["run_mode_at_snapshot"], "testnet_orders")
        self.assertEqual(out["position"], "多")

    def test_heartbeat_is_atomic_json_and_has_mode(self):
        deploy.save_heartbeat(status="running", execute=False, detail="unit test")
        with open(deploy.HEARTBEAT, encoding="utf-8") as fh:
            out = json.load(fh)
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["mode"], "observation_only")
        self.assertEqual(out["detail"], "unit test")
        self.assertEqual(out["symbol"], "BTCUSDT")
        self.assertEqual(out["symbols"], ["BTCUSDT", "ETHUSDT"])
        # 2026-10-03 停用 5m 后心跳只报两条 15m。
        self.assertEqual(out["strategies"], ["kdj15", "eth15"])


class _FakeChaseResult:
    def __init__(self, *, ok, error="", maker=False, avg_price=0.0, steps=0,
                 passive=0):
        self.ok = ok
        self.error = error
        self.avg_price = avg_price
        self.raw = {"chase": {"attempts": [], "steps_used": steps,
                              "final_step": steps - 1,
                              "passive_attempts": passive,
                              "likely_maker": maker}}


class TestChaseNote(unittest.TestCase):
    def test_failed_chase_note_keeps_the_real_reason(self):
        # 2026-10-03: 追价备注曾挡住真实错误 —— 净仓日志显示「taker 被动0次」
        # 而实际是 -2019 保证金不足。失败原因必须出现在同一行。
        r = _FakeChaseResult(
            ok=False, steps=3, passive=3,
            error="passive_exhausted: 被动 3 次 + IOC限价兜底未确认成交; "
                  "Order would immediately match and take: -2019 Margin is insufficient.",
        )
        note = deploy._chase_note(r)
        self.assertIn("被动3次", note)
        self.assertIn("-2019", note)

    def test_ok_chase_note_has_no_error_noise(self):
        # 与真实健康日志同形: maker 被动1次 成交价=84626.00 总单数=15
        r = _FakeChaseResult(ok=True, maker=True, avg_price=84626.0, steps=15,
                             passive=1)
        self.assertEqual(deploy._chase_note(r),
                         "maker 被动1次 成交价=84626.00 总单数=15")


class TestHeartbeatExposesLoadedParams(unittest.TestCase):
    """心跳必须写明进程**实际加载**的参数。

    2026-10-04：RISK_R 从 0.03 改成 0.01 后，从心跳/面板完全看不出线上进程
    其实还在跑旧的 0.03（编辑不热加载）。有了这组字段，比对一眼即可发现
    「进程配置 ≠ 磁盘配置」。
    """

    def _heartbeat(self):
        tmp = tempfile.TemporaryDirectory()
        old_out, old_hb = deploy.OUT, deploy.HEARTBEAT
        deploy.OUT = tmp.name
        deploy.HEARTBEAT = os.path.join(tmp.name, "runner_heartbeat.json")
        try:
            deploy.save_heartbeat(status="running", execute=True)
            with open(deploy.HEARTBEAT, encoding="utf-8") as fh:
                return json.load(fh)
        finally:
            deploy.OUT, deploy.HEARTBEAT = old_out, old_hb
            tmp.cleanup()

    def test_params_block_matches_loaded_module_constants(self):
        rec = self._heartbeat()
        params = rec.get("params") or {}
        self.assertEqual(params.get("risk_r"), deploy.RISK_R)
        self.assertEqual(params.get("margin_budget_per_trade"),
                         deploy.MARGIN_BUDGET_PER_TRADE)
        self.assertEqual(params.get("leverage"), deploy.LEVERAGE)
        self.assertEqual(params.get("max_layers"),
                         {spec.id: spec.max_layers for spec in deploy.SPECS})

    def test_heartbeat_carries_pid_start_time_and_fingerprint(self):
        rec = self._heartbeat()
        self.assertEqual(rec.get("pid"), os.getpid())
        self.assertEqual(rec.get("started_ms"), deploy.PROCESS_STARTED_MS)
        self.assertTrue(rec.get("params_fingerprint"))

    def test_fingerprint_is_stable_and_changes_with_params(self):
        base = deploy.runtime_params()
        self.assertEqual(deploy.params_fingerprint(base),
                         deploy.params_fingerprint(dict(base)))
        changed = dict(base, risk_r=base.get("risk_r", 0) + 1)
        self.assertNotEqual(deploy.params_fingerprint(base),
                            deploy.params_fingerprint(changed))


class TestEmptyTradeLogGetsHeader(unittest.TestCase):
    """0 字节的台账文件也必须补表头。

    2026-10-04：系统重置脚本留下空文件，而 log_row 只判「文件是否存在」，
    于是表头永远缺失 → scripts/monitor_shadow.sh 每小时 KeyError('动作') 崩一次。
    """

    def _row_count(self, path):
        with open(path, encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def test_zero_byte_file_still_gets_the_header(self):
        tmp = tempfile.TemporaryDirectory()
        old_log = deploy.TRADE_LOG
        path = os.path.join(tmp.name, "deployed_trades.csv")
        open(path, "w").close()                     # 0 字节空文件
        deploy.TRADE_LOG = path
        try:
            deploy.log_row(["x"] * len(deploy.COLS))
            rows = self._row_count(path)
        finally:
            deploy.TRADE_LOG = old_log
            tmp.cleanup()
        self.assertEqual(rows[0], deploy.COLS, "空文件必须补上表头")
        self.assertEqual(len(rows), 2)

    def test_existing_file_with_content_is_not_duplicated(self):
        tmp = tempfile.TemporaryDirectory()
        old_log = deploy.TRADE_LOG
        path = os.path.join(tmp.name, "deployed_trades.csv")
        deploy.TRADE_LOG = path
        try:
            deploy.log_row(["a"] * len(deploy.COLS))
            deploy.log_row(["b"] * len(deploy.COLS))
            rows = self._row_count(path)
        finally:
            deploy.TRADE_LOG = old_log
            tmp.cleanup()
        self.assertEqual(rows[0], deploy.COLS)
        self.assertEqual(len(rows), 3, "表头只能写一次")


class TestMarginSkipRow(unittest.TestCase):
    def test_margin_skip_row_is_twelve_columns_with_msg_in_note(self):
        # 2026-10-04: 该行曾只给 11 列，msg 错位写进「权益」列
        # （monitor.log 实测 权益=保证金不足，跳过开仓...）。
        tmp = tempfile.TemporaryDirectory()
        old_log = deploy.TRADE_LOG
        deploy.TRADE_LOG = os.path.join(tmp.name, "deployed_trades.csv")
        try:
            deploy.log_row(deploy.margin_skip_row(
                now_ms=1759500000000, symbol="BTCUSDT", ex=0.0, mark=60000.0,
                msg="保证金不足，跳过开仓：需 100.00 > 可用 50.00"))
            with open(deploy.TRADE_LOG, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        finally:
            deploy.TRADE_LOG = old_log
            tmp.cleanup()
        self.assertEqual(rows[0], deploy.COLS)
        row = rows[1]
        self.assertEqual(len(row), 12)
        self.assertEqual(row[10], "")
        self.assertIn("保证金不足", row[11])


if __name__ == "__main__":
    unittest.main()
