"""提示音不得打断交易路径。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.models import OrderResult
from trading.binance_client import BinanceTestnetClient
from utils import alert_sound


class TestAlertSound(unittest.TestCase):
    def test_disabled_inside_unittest(self):
        self.assertFalse(alert_sound.alerts_enabled())

    def test_play_alert_never_raises(self):
        alert_sound.play_alert("order")
        alert_sound.play_alert("open")
        alert_sound.play_alert("unknown")

    def test_open_sound_only_on_opening_fill(self):
        filled = OrderResult(
            ok=True, symbol="BTCUSDT", cum_filled_qty=0.047, avg_price=84700.0
        )
        with patch("utils.alert_sound.play_alert") as play:
            out = BinanceTestnetClient._finish_passive(
                filled, [], maker=True, reduce_only=False
            )
        self.assertTrue(out.ok)
        play.assert_called_once_with("open")

    def test_close_fill_does_not_play_open_sound(self):
        filled = OrderResult(
            ok=True, symbol="BTCUSDT", cum_filled_qty=0.047, avg_price=84700.0
        )
        with patch("utils.alert_sound.play_alert") as play:
            BinanceTestnetClient._finish_passive(
                filled, [], maker=True, reduce_only=True
            )
        play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
