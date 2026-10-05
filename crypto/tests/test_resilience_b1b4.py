"""B1–B4 阻塞项修复的单元测试。

覆盖 2026-10-04 验收报告点名的四个机制缺失：
    B1  单实例锁       —— 第二个实例必须被拒绝
    B2  状态损坏自愈   —— 损坏不得崩进程，必须回退备份
    B2  状态落盘       —— fsync + 备份轮转
    B3  外部告警       —— 不阻塞、不抛异常、冷却去重、本地必落盘

全部用临时目录，不碰生产状态文件。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow import alerts
from shadow import deploy


class TempStateMixin:
    """把 deploy 的状态路径重定向到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b1b4-")
        self.state = os.path.join(self.tmp, "deployed_state.json")
        self.bak = self.state + ".bak"
        self.lock = os.path.join(self.tmp, "trader.lock")
        self._patches = [
            mock.patch.object(deploy, "OUT", self.tmp),
            mock.patch.object(deploy, "STATE", self.state),
            mock.patch.object(deploy, "STATE_BAK", self.bak),
            mock.patch.object(deploy, "LOCK", self.lock),
        ]
        for p in self._patches:
            p.start()
        # 告警落盘也隔离，避免污染真实 alerts.jsonl
        self.alert_log = os.path.join(self.tmp, "alerts.jsonl")
        self._alert_patch = mock.patch.object(alerts, "ALERT_LOG",
                                              self.alert_log)
        self._alert_patch.start()
        alerts.reset_cooldown()

    def tearDown(self):
        self._alert_patch.stop()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# B2：状态损坏自愈
# --------------------------------------------------------------------------
class TestStateRecovery(TempStateMixin, unittest.TestCase):

    def test_missing_file_uses_default(self):
        st = deploy.load_state()
        self.assertIsInstance(st, dict)
        self.assertIn("last_ts", st)

    def test_valid_file_is_read(self):
        payload = deploy._default_state()
        payload["last_ts"] = 123456
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        st = deploy.load_state()
        self.assertEqual(int(st.get("last_ts") or 0), 123456)

    def test_truncated_json_does_not_crash_and_falls_back_to_backup(self):
        """核心回归：截断的 JSON 曾经会直接崩掉进程 → 崩溃循环。"""
        good = deploy._default_state()
        good["last_ts"] = 999
        with open(self.bak, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write('{"last_ts": 5, "peak": 0.0')      # 截断

        st = deploy.load_state()                        # 不得抛异常
        self.assertEqual(int(st.get("last_ts") or 0), 999,
                         "必须回退到 .bak 的值")

    def test_corrupt_file_is_quarantined(self):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        deploy.load_state()
        quarantined = [f for f in os.listdir(self.tmp)
                       if f.startswith("deployed_state.json.corrupt-")]
        self.assertEqual(len(quarantined), 1, "损坏文件必须留证而不是删掉")

    def test_empty_file_falls_back(self):
        good = deploy._default_state()
        good["last_ts"] = 777
        with open(self.bak, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        open(self.state, "w").close()                   # 0 字节
        st = deploy.load_state()
        self.assertEqual(int(st.get("last_ts") or 0), 777)

    def test_no_backup_still_recovers_to_default(self):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("{ broken")
        st = deploy.load_state()                        # 不得抛异常
        self.assertIsInstance(st, dict)

    def test_corrupt_backup_is_not_trusted(self):
        with open(self.bak, "w", encoding="utf-8") as fh:
            fh.write("{ also broken")
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("{ broken")
        st = deploy.load_state()
        self.assertIsInstance(st, dict)


# --------------------------------------------------------------------------
# B2：保存与备份轮转
# --------------------------------------------------------------------------
class TestStateSave(TempStateMixin, unittest.TestCase):

    def test_save_then_load_roundtrip(self):
        st = deploy._default_state()
        st["last_ts"] = 42
        deploy.save_state(st)
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual(int(deploy.load_state().get("last_ts") or 0), 42)

    def test_second_save_rotates_backup(self):
        a = deploy._default_state(); a["last_ts"] = 1
        deploy.save_state(a)
        b = deploy._default_state(); b["last_ts"] = 2
        deploy.save_state(b)
        self.assertTrue(os.path.exists(self.bak), "第二次保存应轮转出备份")
        with open(self.bak, encoding="utf-8") as fh:
            self.assertEqual(int(json.load(fh).get("last_ts") or 0), 1)

    def test_no_tmp_left_behind(self):
        deploy.save_state(deploy._default_state())
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], "临时文件必须被 rename 掉")

    def test_corrupt_existing_state_is_not_copied_over_backup(self):
        """关键：损坏内容绝不能被写进备份，否则会把好备份毁掉。"""
        good = deploy._default_state(); good["last_ts"] = 111
        with open(self.bak, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("{ truncated")
        deploy.save_state(deploy._default_state())
        with open(self.bak, encoding="utf-8") as fh:
            self.assertEqual(int(json.load(fh).get("last_ts") or 0), 111,
                             "备份必须保持完好")


# --------------------------------------------------------------------------
# B1：单实例锁
# --------------------------------------------------------------------------
class TestSingleInstance(TempStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        deploy._LOCK_FH = None

    def tearDown(self):
        if deploy._LOCK_FH is not None:
            try:
                deploy._LOCK_FH.close()
            except OSError:
                pass
            deploy._LOCK_FH = None
        super().tearDown()

    def test_first_acquire_succeeds(self):
        self.assertTrue(deploy.acquire_single_instance_lock())
        self.assertIsNotNone(deploy._LOCK_FH)

    def test_second_acquire_in_same_process_is_blocked(self):
        """同一进程内第二次取锁必须失败（flock 语义：同一 fd 可重入，
        所以这里模拟的是「另一个持有者」——用独立 fd 验证互斥）。"""
        self.assertTrue(deploy.acquire_single_instance_lock())
        import fcntl
        other = open(self.lock, "a+")
        try:
            with self.assertRaises(OSError):
                fcntl.flock(other.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            other.close()

    def test_lock_file_records_pid(self):
        self.assertTrue(deploy.acquire_single_instance_lock())
        with open(self.lock, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(int(rec["pid"]), os.getpid())
        self.assertIn("argv", rec)

    def test_lock_released_when_holder_dies(self):
        """flock 的核心优势：进程死亡（含 kill -9）内核自动释放，不留死锁。"""
        import fcntl
        holder = open(self.lock, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        holder.close()                                  # 等价于进程退出
        self.assertTrue(deploy.acquire_single_instance_lock(),
                        "持有者消失后必须能重新取锁")

    def test_blocked_acquire_emits_critical_alert(self):
        import fcntl
        # 用一个独立 fd 模拟「另一个实例已经持有锁」。flock 是按
        # open-file-description 互斥的，所以同进程的另一个 fd 也会冲突。
        other = open(self.lock, "a+")
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        alerts.reset_cooldown()
        try:
            ok = deploy.acquire_single_instance_lock()
        finally:
            other.close()
        self.assertFalse(ok)
        with open(self.alert_log, encoding="utf-8") as fh:
            keys = [json.loads(x)["key"] for x in fh if x.strip()]
        self.assertIn("duplicate_instance_blocked", keys)


# --------------------------------------------------------------------------
# B3：告警通道
# --------------------------------------------------------------------------
class TestAlerts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alerts-")
        self.log = os.path.join(self.tmp, "alerts.jsonl")
        self._p = mock.patch.object(alerts, "ALERT_LOG", self.log)
        self._p.start()
        alerts.reset_cooldown()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("CRYPTO_ALERT_WEBHOOK", "CRYPTO_ALERT_BARK",
                  "CRYPTO_ALERT_OSASCRIPT"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_local_log(self):
        alerts.notify("WARNING", "k1", "标题", "细节")
        with open(self.log, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["level"], "WARNING")
        self.assertEqual(rec["title"], "标题")
        self.assertEqual(rec["key"], "k1")

    def test_local_log_not_cooldown_limited(self):
        """落盘不冷却 —— 记录必须完整；只有远端发送才去重。"""
        for _ in range(5):
            alerts.notify("WARNING", "same", "t", "d")
        with open(self.log, encoding="utf-8") as fh:
            self.assertEqual(len([x for x in fh if x.strip()]), 5)

    def test_cooldown_suppresses_remote(self):
        self.assertTrue(alerts.notify("WARNING", "k2", "t", "d"))
        self.assertFalse(alerts.notify("WARNING", "k2", "t", "d"),
                         "冷却期内不应重复发送")

    def test_different_keys_are_independent(self):
        self.assertTrue(alerts.notify("WARNING", "a", "t", "d"))
        self.assertTrue(alerts.notify("WARNING", "b", "t", "d"))

    def test_never_raises_on_bad_input(self):
        alerts.notify("NOT_A_LEVEL", "", "", "")
        alerts.notify("INFO", "k3", "t", "d", extra_object=object())

    def test_illegal_level_degrades_to_warning(self):
        alerts.notify("SUPER_BAD", "k4", "t", "d")
        with open(self.log, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["level"], "WARNING")

    def test_disabled_short_circuits(self):
        with mock.patch.object(alerts, "_DISABLED", True):
            self.assertFalse(alerts.notify("INFO", "k5", "t", "d"))
        self.assertFalse(os.path.exists(self.log))

    def test_unwritable_log_does_not_raise(self):
        with mock.patch.object(alerts, "ALERT_LOG",
                               "/proc/definitely/not/writable/x.jsonl"):
            alerts.notify("INFO", "k6", "t", "d")       # 不得抛异常


if __name__ == "__main__":
    unittest.main()
