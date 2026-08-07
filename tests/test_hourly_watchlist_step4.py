from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from backend import hourly_watchlist


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {"ok": True, "degraded": False}

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def put(self, key, value):
        self.values[key] = copy.deepcopy(value)


class FakeCore:
    def __init__(self, now):
        self.now = now
        self._durable_state_store = MemoryStore()
        self.fetch_count = 0

    def public_bybit_get(self, path, params):
        assert path == "/v5/market/tickers"
        rows = []
        for index in range(25):
            rows.append(
                {
                    "symbol": f"COIN{index:03d}USDT",
                    "lastPrice": str(10 + index),
                    "turnover24h": str(20_000_000 + index),
                    "bid1Price": "10.00",
                    "ask1Price": "10.01",
                }
            )
        return {"retCode": 0, "result": {"list": rows}}

    def fetch_candles(self, symbol, interval, limit=80):
        assert interval == "60"
        self.fetch_count += 1
        target = hourly_watchlist._target_candle_open_seconds(self.now) * 1000
        duration = 3_600_000
        candles = []
        for index in range(65):
            candles.append(
                {
                    "time": target - ((64 - index) * duration),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "volume": 1000.0,
                    "trend": "BULLISH",
                    "score": 100 - int(symbol[4:7]),
                }
            )
        return candles[-limit:], "OK"


def fresh_worker():
    class Worker:
        @staticmethod
        def classify_trend(candles):
            last = candles[-1]
            return last["trend"], float(last["score"]), "existing worker trend"

        @staticmethod
        def _rank_rows(rows):
            for row in rows:
                row["rankScore"] = float(row["trendScore"])
            return sorted(rows, key=lambda row: row["rankScore"], reverse=True)

        @staticmethod
        def run_batch(core, now=None):
            return {"status": "base"}

        @staticmethod
        def snapshot():
            return {"status": "base"}

    return Worker


class HourlyWatchlistTests(unittest.TestCase):
    def setUp(self):
        hourly_watchlist._reset_for_tests()

    def tearDown(self):
        hourly_watchlist._reset_for_tests()

    def test_direct_market_to_closed_1h_top20(self):
        now = int(datetime(2026, 8, 3, 10, 5, tzinfo=timezone.utc).timestamp())
        core = FakeCore(now)
        worker = fresh_worker()
        hourly_watchlist.install(core, worker)
        result = hourly_watchlist.build(core, now=now)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["eligibleMarketInput"], 25)
        self.assertEqual(result["metrics"]["oneHourQualified"], 25)
        self.assertEqual(result["metrics"]["selected"], 20)
        self.assertFalse(result["metrics"]["dailyGateRequired"])
        self.assertFalse(result["metrics"]["fourHourGateRequired"])
        self.assertEqual(result["metrics"]["upstreamTimeframes"], ["1H"])
        self.assertEqual(result["source"], "eligible_usdt_closed_1h_top20")
        self.assertTrue(result["persisted"])

    def test_no_daily_or_four_hour_core_dependency(self):
        now = int(datetime(2026, 8, 3, 11, 5, tzinfo=timezone.utc).timestamp())
        core = FakeCore(now)
        self.assertFalse(hasattr(core, "daily_master_universe"))
        self.assertFalse(hasattr(core, "four_hour_directional_pool"))
        worker = fresh_worker()
        hourly_watchlist.install(core, worker)
        result = hourly_watchlist.build(core, now=now)
        self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()
