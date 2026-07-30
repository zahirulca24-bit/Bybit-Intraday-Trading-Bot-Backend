import unittest
from types import SimpleNamespace

from backend.batch1_safety import fail_closed_daily_risk, normalize_symbol, validate_start_payload


class Batch1ExecutionSafetyTests(unittest.TestCase):
    def test_symbol_must_remain_exact_valid_usdt_symbol(self):
        self.assertEqual(normalize_symbol("ethusdt"), "ETHUSDT")
        with self.assertRaisesRegex(ValueError, "valid USDT"):
            normalize_symbol("NOT-A-SYMBOL")

    def test_start_payload_rejects_extreme_risk(self):
        defaults = {
            "symbol": "BTCUSDT",
            "interval": "5",
            "riskPerTradePct": 0.25,
            "maxAllocationUsdt": 250,
            "maxOpenPositions": 1,
            "dailyLossCapUsdt": 25,
            "maxTradesPerDay": 6,
            "stopLossPct": 0.8,
            "takeProfitPct": 1.6,
            "breakevenTriggerPct": 0.6,
            "partialTpTriggerPct": 1.4,
            "partialTpClosePct": 40,
            "trailingStopTriggerPct": 1.8,
            "trailingStopDistancePct": 0.45,
            "cooldownSeconds": 180,
            "mode": "conservative",
        }
        with self.assertRaisesRegex(ValueError, "riskPerTradePct"):
            validate_start_payload({"riskPerTradePct": 99}, defaults)
        with self.assertRaisesRegex(ValueError, "maxAllocationUsdt"):
            validate_start_payload({"maxAllocationUsdt": 100000}, defaults)
        with self.assertRaisesRegex(ValueError, "maxOpenPositions"):
            validate_start_payload({"maxOpenPositions": 50}, defaults)

    def test_daily_risk_api_failure_blocks_execution(self):
        core = SimpleNamespace(
            get_current_trading_date_key=lambda: "2026-07-25",
            get_daily_closed_pnl=lambda _: (None, "Bybit unavailable"),
        )
        blocked, reason = fail_closed_daily_risk(core, {"dailyLossCapUsdt": 25, "maxTradesPerDay": 6})
        self.assertTrue(blocked)
        self.assertIn("execution blocked", reason)

    def test_daily_loss_blocks_but_trade_count_does_not(self):
        core = SimpleNamespace(
            get_current_trading_date_key=lambda: "2026-07-25",
            get_daily_closed_pnl=lambda _: (-30.0, "OK"),
        )
        blocked, reason = fail_closed_daily_risk(core, {"dailyLossCapUsdt": 25, "maxTradesPerDay": 6})
        self.assertTrue(blocked)
        self.assertIn("Daily loss cap reached", reason)

        core.get_daily_closed_pnl = lambda _: (0.0, "OK")
        blocked, reason = fail_closed_daily_risk(core, {"dailyLossCapUsdt": 25, "maxTradesPerDay": 6})
        self.assertFalse(blocked)
        self.assertEqual(reason, "Daily risk OK; trade count unlimited")


if __name__ == "__main__":
    unittest.main()
