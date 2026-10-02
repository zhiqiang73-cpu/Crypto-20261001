"""运行器读数/心跳快照测试：不连接交易所、不提交委托。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
