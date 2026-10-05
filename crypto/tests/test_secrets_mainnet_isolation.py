"""主网/测试网凭据隔离 + 原子写入测试。

守住的约束（2026-10-05 用户要求）：
1. 主网凭据与测试网凭据使用**不同键名**，互不覆盖；
2. 写入是原子的（临时文件 + os.replace），且权限为 0600；
3. secrets 文件被 .gitignore 排除，不会进入备份。
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import secrets as secrets_mod


class SecretsMainnetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets.json"
        patcher = mock.patch.object(secrets_mod, "SECRETS_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 清掉可能存在的环境变量覆盖，确保测的是文件路径
        for env in ("BINANCE_MAINNET_API_KEY", "BINANCE_MAINNET_API_SECRET",
                    "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"):
            mock.patch.dict(os.environ, {env: ""}, clear=False).start()

    def test_mainnet_keys_are_allowed_and_distinct(self):
        self.assertIn("binance_mainnet_api_key", secrets_mod.ALLOWED_KEYS)
        self.assertIn("binance_mainnet_api_secret", secrets_mod.ALLOWED_KEYS)
        self.assertIn("binance_testnet_api_key", secrets_mod.ALLOWED_KEYS)
        self.assertNotEqual(
            secrets_mod.ENV_MAP["binance_mainnet_api_key"],
            secrets_mod.ENV_MAP["binance_testnet_api_key"],
        )

    def test_mainnet_save_does_not_touch_testnet_credentials(self):
        secrets_mod.save_secrets({
            "binance_testnet_api_key": "testnet-key",
            "binance_testnet_api_secret": "testnet-secret",
        })
        secrets_mod.save_secrets({
            "binance_mainnet_api_key": "mainnet-key",
            "binance_mainnet_api_secret": "mainnet-secret",
        })
        loaded = secrets_mod.load_secrets()
        self.assertEqual(loaded["binance_testnet_api_key"], "testnet-key")
        self.assertEqual(loaded["binance_mainnet_api_key"], "mainnet-key")
        self.assertEqual(loaded["binance_mainnet_api_secret"], "mainnet-secret")

    def test_forget_mainnet_keeps_testnet(self):
        secrets_mod.save_secrets({
            "binance_testnet_api_key": "testnet-key",
            "binance_mainnet_api_key": "mainnet-key",
            "binance_mainnet_api_secret": "mainnet-secret",
        })
        secrets_mod.save_secrets({
            "binance_mainnet_api_key": None,
            "binance_mainnet_api_secret": None,
        })
        loaded = secrets_mod.load_secrets()
        self.assertNotIn("binance_mainnet_api_key", loaded)
        self.assertEqual(loaded["binance_testnet_api_key"], "testnet-key")

    def test_unknown_keys_are_rejected(self):
        secrets_mod.save_secrets({"evil_key": "x", "binance_mainnet_api_key": "k"})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("evil_key", data)

    def test_write_is_atomic_and_0600(self):
        secrets_mod.save_secrets({"binance_mainnet_api_key": "k"})
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name.startswith(".secrets.")]
        self.assertEqual(leftovers, [], "临时文件必须被清理")

    def test_secrets_file_is_gitignored(self):
        root = Path(__file__).resolve().parents[1]
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("runtime/secrets.json", ignored)


if __name__ == "__main__":
    unittest.main()
