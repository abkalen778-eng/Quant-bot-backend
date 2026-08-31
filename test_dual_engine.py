import unittest
from unittest.mock import patch

import main


def signal(strategy, key, price=100.0):
    return {
        "product": "BTC-USD",
        "strategy": strategy,
        "signal": "BULLISH",
        "trade_ready": True,
        "price": price,
        "signal_key": key,
        "stop_loss": {"price": 95.0},
        "take_profit": {"price": 110.0} if strategy == "tjr_core_v1" else None,
    }


class DualEngineTests(unittest.TestCase):
    def setUp(self):
        self.old_products = main.AUTO_PRODUCTS
        self.old_tjr_live = main.TJR_LIVE_ENABLED
        main.AUTO_PRODUCTS = ["BTC-USD"]
        main.TJR_LIVE_ENABLED = False
        with main.state_lock:
            main.bot_state["last_results"] = []
            main.bot_state["dry_run_actions"] = []
            main.bot_state["processed_signal_keys"] = []
            main.bot_state["paper_positions"] = {}
            main.bot_state["paper_closed_trades"] = []

    def tearDown(self):
        main.AUTO_PRODUCTS = self.old_products
        main.TJR_LIVE_ENABLED = self.old_tjr_live

    @patch("main._update_paper_positions")
    @patch("main._execute")
    @patch("main.build_strategy_signals")
    def test_tjr_is_paper_only_until_live_exit_gate_is_enabled(self, signals, execute, _update):
        signals.return_value = [signal("tjr_core_v1", "tjr-key")]
        main._algo_cycle()
        execute.assert_not_called()
        action = main.bot_state["dry_run_actions"][-1]
        self.assertEqual(action["mode"], "PAPER_TEST")
        self.assertFalse(action["executed"])
        self.assertIn("BTC-USD", main.bot_state["paper_positions"])

    @patch("main._update_paper_positions")
    @patch("main._execute")
    @patch("main.build_strategy_signals")
    def test_strategies_cannot_open_duplicate_product_positions(self, signals, execute, _update):
        signals.return_value = [
            signal("confirmed_breakout_v1", "breakout-key"),
            signal("tjr_core_v1", "tjr-key"),
        ]
        execute.return_value = {"accepted": True, "executed": False, "mode": "DRY_RUN"}
        main._algo_cycle()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(main.bot_state["paper_positions"]), 1)
        actions = main.bot_state["dry_run_actions"]
        self.assertEqual(actions[-1]["action"], "OPEN_POSITION_EXISTS")


if __name__ == "__main__":
    unittest.main()
