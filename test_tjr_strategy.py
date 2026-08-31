import unittest

from tjr_strategy import evaluate_tjr_core


def candle(time, open_=100.0, high=101.0, low=99.0, close=100.0, volume=10.0):
    return {
        "time": float(time),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


def valid_fixture():
    daily = [
        candle(0, high=118, low=92),
        candle(86400, high=120, low=90),
    ]
    five = [candle(165000 + i * 300) for i in range(50)]
    five[18] = candle(five[18]["time"], high=103, low=99, close=101)
    five[19] = candle(five[19]["time"], high=104, low=100, close=102)
    five[20] = candle(five[20]["time"], high=105, low=101, close=103)
    five[21] = candle(five[21]["time"], high=104, low=100, close=102)
    five[22] = candle(five[22]["time"], high=103, low=99, close=101)
    five[30] = candle(five[30]["time"], high=92, low=89, close=91)
    five[32] = candle(five[32]["time"], high=107, low=101, close=106)

    one_start = int(five[32]["time"] + 300)
    before = [candle(one_start - (30 - i) * 60) for i in range(30)]
    after = [candle(one_start + i * 60, high=102, low=100, close=101) for i in range(20)]
    after[2] = candle(after[2]["time"], high=102, low=99, close=100)
    after[5] = candle(after[5]["time"], high=100, low=97, close=98)
    after[6] = candle(after[6]["time"], high=102, low=98, close=101)
    after[7] = candle(after[7]["time"], high=104, low=100, close=102)
    after[8] = candle(after[8]["time"], high=103, low=100, close=102)
    after[9] = candle(after[9]["time"], high=102.5, low=100, close=101)
    after[10] = candle(after[10]["time"], high=106, low=101, close=105)
    one = before + after
    now = after[10]["time"] + 360
    return daily, five, one, now


class TjrCoreTests(unittest.TestCase):
    def test_requires_liquidity_sweep(self):
        daily, five, one, now = valid_fixture()
        five[30] = candle(five[30]["time"], high=93, low=90.5, close=92)
        result = evaluate_tjr_core("BTC-USD", daily, five, one, 110, now)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["trade_ready"])

    def test_complete_sequence_is_bullish(self):
        daily, five, one, now = valid_fixture()
        result = evaluate_tjr_core("BTC-USD", daily, five, one, 110, now)
        self.assertEqual(result["signal"], "BULLISH")
        self.assertTrue(result["trade_ready"])
        self.assertIn(result["confirmation"], {"5m_bos", "5m_inverse_fvg"})
        self.assertLess(result["stop_loss"]["price"], result["price"])
        self.assertGreater(result["take_profit"]["price"], result["price"])

    def test_stale_sequence_is_rejected(self):
        daily, five, one, now = valid_fixture()
        result = evaluate_tjr_core("BTC-USD", daily, five, one, 110, now + 1200)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertFalse(result["trade_ready"])

    def test_never_generates_short_entry(self):
        daily, five, one, now = valid_fixture()
        daily[-1] = candle(86400, high=120, low=90)
        for row in five:
            row["high"] = min(row["high"], 119)
        five[30] = candle(five[30]["time"], high=121, low=117, close=119)
        result = evaluate_tjr_core("BTC-USD", daily, five, one, 110, now)
        self.assertNotEqual(result["signal"], "BEARISH")


if __name__ == "__main__":
    unittest.main()
