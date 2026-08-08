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
    def __init__(self, now, count=60):
        self.now = now
        self.count = count
        self._durable_state_store = MemoryStore()
        self.fetch_count = 0

    def public_bybit_get(self, path, params):
        assert path == "/v5/market/tickers"
        rows = []
        for index in range(self.count):
            last = 10 + index
            rows.append(
                {
                    "symbol": f"COIN{index:03d}USDT",
                    "lastPrice": str(last),
                    "turnover24h": str(20_000_000 + index),
                    "bid1Price": str(last),
                    "ask1Price": str(last + 0.001),
                    "price24hPcnt": "0.01",
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

    def test_direct_market_to_closed_1h_top50(self):
        now = int(datetime(2026, 8, 3, 10, 5, tzinfo=timezone.utc).timestamp())
        core = FakeCore(now, count=60)
        worker = fresh_worker()
        hourly_watchlist.install(core, worker)
        result = hourly_watchlist.build(core, now=now)

        self.assertEqual(hourly_watchlist.settings()["watchlistSize"], 50)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["eligibleMarketInput"], 60)
        self.assertEqual(result["metrics"]["oneHourQualified"], 60)
        self.assertEqual(result["metrics"]["selected"], 50)
        self.assertEqual(len(result["rows"]), 50)
        self.assertEqual(result["metrics"]["upstreamTimeframes"], ["1H"])
        self.assertEqual(result["source"], "eligible_usdt_closed_1h_top50")
        self.assertEqual(hourly_watchlist.status(worker)["policy"], "ELIGIBLE_USDT_TO_CLOSED_1H_TOP50")
        self.assertTrue(result["persisted"])

    def test_legacy_top20_persistence_loads_safely(self):
        now = int(datetime(2026, 8, 3, 11, 5, tzinfo=timezone.utc).timestamp())
        core = FakeCore(now)
        old_rows = [
            {
                "symbol": f"OLD{index:02d}USDT",
                "trend": "BULLISH",
                "oneHourCandleTime": 123,
            }
            for index in range(20)
        ]
        core._durable_state_store.put(
            "hourly_watchlist_top20_v2",
            {
                "status": "ready",
                "version": 2,
                "source": "eligible_usdt_closed_1h_top20",
                "oneHourCandleTime": 123,
                "updatedAt": 100,
                "symbols": [row["symbol"] for row in old_rows],
                "rows": old_rows,
                "metrics": {"selected": 20},
                "lastError": None,
            },
        )
        worker = fresh_worker()
        hourly_watchlist.install(core, worker)
        loaded = hourly_watchlist.snapshot()

        self.assertEqual(len(loaded["rows"]), 20)
        self.assertEqual(loaded["source"], "eligible_usdt_closed_1h_top50")
        self.assertEqual(loaded["metrics"]["migratedFrom"], "hourly_watchlist_top20_v2")

    def test_market_source_requires_no_parent_timeframe_stage(self):
        now = int(datetime(2026, 8, 3, 11, 5, tzinfo=timezone.utc).timestamp())
        core = FakeCore(now)
        worker = fresh_worker()
        hourly_watchlist.install(core, worker)
        result = hourly_watchlist.build(core, now=now)
        self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()
