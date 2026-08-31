import unittest
from unittest.mock import patch

import main


def signal(strategy, key="signal-key", price=100.0):
    return {
        "product": "BTC-USD",
        "strategy": strategy,
        "signal": "BULLISH",
        "trade_ready": True,
        "price": price,
        "signal_key": key,
        "stop_loss": {"price": 95.0},
        "take_profit": {"price": 110.0} if strategy == "tjr_core_v1" else None,
        "exit_plan": {"stop_price": 95.0, "target_price": 110.0},
    }


class StrategySwitchTests(unittest.TestCase):
    def setUp(self):
        self.old_strategy = main.STRATEGY_NAME
        self.old_tjr_live = main.TJR_LIVE_ENABLED

    def tearDown(self):
        main.STRATEGY_NAME = self.old_strategy
        main.TJR_LIVE_ENABLED = self.old_tjr_live

    @patch("main.build_tjr_core_signal")
    def test_tjr_is_the_only_selected_strategy(self, tjr):
        main.STRATEGY_NAME = "tjr_core_v1"
        tjr.return_value = signal("tjr_core_v1")
        results = main.build_strategy_signals("BTC-USD")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["strategy"], "tjr_core_v1")

    @patch("main._client")
    @patch("main.build_signal")
    def test_tjr_live_gate_blocks_coinbase_submission(self, build_signal, client):
        main.TJR_LIVE_ENABLED = False
        build_signal.return_value = signal("tjr_core_v1")
        result = main._execute("BTC-USD", "BUY", 5.0, True)
        self.assertEqual(result["mode"], "PAPER_TEST")
        self.assertFalse(result["executed"])
        client.assert_not_called()

    @patch("main.build_confirmed_breakout_signal")
    def test_switch_can_restore_breakout(self, breakout):
        main.STRATEGY_NAME = "confirmed_breakout_v1"
        breakout.return_value = signal("confirmed_breakout_v1")
        result = main.build_signal("BTC-USD")
        self.assertEqual(result["strategy"], "confirmed_breakout_v1")


if __name__ == "__main__":
    unittest.main()
