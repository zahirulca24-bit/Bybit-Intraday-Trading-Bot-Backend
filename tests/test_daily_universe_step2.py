from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend import daily_universe


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {
            "ok": True,
            "degraded": False,
            "persistentPathConfigured": True,
        }

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def put(self, key, value):
        self.values[key] = copy.deepcopy(value)


class FakeCore:
    def __init__(self, rows_by_symbol, now):
        self._durable_state_store = MemoryStore()
        self.rows_by_symbol = rows_by_symbol
        self.now = now

    def fetch_candles(self, symbol, interval, limit=80):
        row = self.rows_by_symbol[symbol][interval]
        duration = 86_400_000 if interval == "D" else 14_400_000
        now_ms = self.now * 1000
        candles = []
        for index in range(65):
            candles.append(
                {
                    "time": now_ms - ((66 - index) * duration),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "volume": 1000.0,
                    "trend": row["trend"],
                    "score": row["score"],
                }
            )
        return candles[-limit:], "OK"


class DailyUniverseStep2Tests(unittest.TestCase):
    def setUp(self):
        daily_universe._reset_for_tests()

    def tearDown(self):
        daily_universe._reset_for_tests()

    @staticmethod
    def classifier(candles):
        last = candles[-1]
        return last["trend"], float(last["score"]), "test trend"

    def test_selects_top_100_aligned_symbols_and_persists_snapshot(self):
        now = int(datetime(2026, 8, 3, 0, 6, tzinfo=timezone.utc).timestamp())
        symbols = [f"COIN{index:03d}USDT" for index in range(105)]
        tickers = {
            symbol: {
                "lastPrice": 1.0 + index,
                "turnover24h": 1_000_000 + index,
                "spreadPct": 0.05,
            }
            for index, symbol in enumerate(symbols)
        }
        rows = {
            symbol: {
                "D": {"trend": "BULLISH", "score": index / 104 * 100},
                "240": {"trend": "BULLISH", "score": index / 104 * 100},
            }
            for index, symbol in enumerate(symbols)
        }
        calls = {"base": 0}

        class Worker:
            @staticmethod
            def _fetch_active_usdt_symbols(core):
                calls["base"] += 1
                return list(symbols), copy.deepcopy(tickers)

            classify_trend = staticmethod(self.classifier)

        core = FakeCore(rows, now)
        with patch.dict(os.environ, {"DAILY_UNIVERSE_SIZE": "100"}, clear=False):
            daily_universe.install(core, Worker)
            result = daily_universe.build(core, now=now)
            selected, selected_tickers = Worker._fetch_active_usdt_symbols(core)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["eligibleInput"], 105)
        self.assertEqual(result["metrics"]["trendAligned"], 105)
        self.assertEqual(result["metrics"]["selected"], 100)
        self.assertEqual(len(result["symbols"]), 100)
        self.assertEqual(result["symbols"][0], "COIN104USDT")
        self.assertNotIn("COIN000USDT", result["symbols"])
        self.assertTrue(result["persisted"])
        self.assertEqual(selected, result["symbols"])
        self.assertEqual(set(selected_tickers), set(result["symbols"]))
        self.assertEqual(calls["base"], 1)
        persisted = core._durable_state_store.get("daily_master_universe_v1")
        self.assertEqual(persisted["symbols"], result["symbols"])

    def test_timeframe_conflict_is_rejected_and_existing_source_remains_fallback(self):
        now = int(datetime(2026, 8, 3, 0, 6, tzinfo=timezone.utc).timestamp())
        symbols = [f"COIN{index:03d}USDT" for index in range(10)]
        tickers = {
            symbol: {"lastPrice": 1.0, "turnover24h": 1_000_000, "spreadPct": 0.05}
            for symbol in symbols
        }
        rows = {
            symbol: {
                "D": {"trend": "BULLISH", "score": 90},
                "240": {"trend": "BEARISH", "score": 90},
            }
            for symbol in symbols
        }
        calls = {"base": 0}

        class Worker:
            @staticmethod
            def _fetch_active_usdt_symbols(core):
                calls["base"] += 1
                return list(symbols), copy.deepcopy(tickers)

            classify_trend = staticmethod(self.classifier)

        core = FakeCore(rows, now)
        daily_universe.install(core, Worker)
        result = daily_universe.build(core, now=now)
        fallback_symbols, fallback_tickers = Worker._fetch_active_usdt_symbols(core)

        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["metrics"]["rejected"]["timeframeConflict"], 10)
        self.assertEqual(result["symbols"], [])
        self.assertEqual(fallback_symbols, symbols)
        self.assertEqual(fallback_tickers, tickers)
        self.assertGreaterEqual(calls["base"], 2)

    def test_schedule_changes_at_0005_utc(self):
        before = int(datetime(2026, 8, 3, 0, 4, tzinfo=timezone.utc).timestamp())
        after = int(datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc).timestamp())

        self.assertEqual(daily_universe._target_run_day(before), "2026-08-02")
        self.assertEqual(daily_universe._target_run_day(after), "2026-08-03")


if __name__ == "__main__":
    unittest.main()
