import unittest

from backend.scanner_safety import (
    bounded_symbols,
    deadline_reached,
    filter_closed_candles,
    normalize_interval,
    signal_identity,
)


class ScannerSafetyTests(unittest.TestCase):
    def test_forming_candle_is_removed(self):
        now_ms = 1_000_000
        candles = [
            {"time": 400_000, "close": 1},
            {"time": 800_000, "close": 2},
        ]
        self.assertEqual(filter_closed_candles(candles, "5", now_ms), [candles[0]])

    def test_requested_interval_is_validated(self):
        self.assertEqual(normalize_interval("15"), "15")
        with self.assertRaises(ValueError):
            normalize_interval("7")

    def test_symbols_are_normalized_deduplicated_and_capped(self):
        self.assertEqual(
            bounded_symbols(["btcusdt", "ETHUSDT", "BTCUSDT", "solusdt"], 2),
            ["BTCUSDT", "ETHUSDT"],
        )

    def test_signal_identity_is_candle_specific(self):
        first = signal_identity("btcusdt", "5", 1000, "buy")
        second = signal_identity("btcusdt", "5", 2000, "buy")
        self.assertEqual(first, "BTCUSDT:5:1000:Buy")
        self.assertNotEqual(first, second)

    def test_deadline_uses_injected_clock(self):
        self.assertTrue(deadline_reached(10.0, 5.0, clock=lambda: 15.0))
        self.assertFalse(deadline_reached(10.0, 5.0, clock=lambda: 14.9))


if __name__ == "__main__":
    unittest.main()
