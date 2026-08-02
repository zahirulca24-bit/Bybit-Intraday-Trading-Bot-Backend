from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend import four_hour_directional_pool


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
    def __init__(self, rows_by_symbol, daily_payload, now):
        self._durable_state_store = MemoryStore()
        self.rows_by_symbol = rows_by_symbol
        self.daily_payload = daily_payload
        self.now = now

    def daily_master_universe(self, force=False):
        return copy.deepcopy(self.daily_payload)

    def fetch_candles(self, symbol, interval, limit=80):
        self.assert_interval(interval)
        row = self.rows_by_symbol[symbol]
        duration = 14_400_000
        target = four_hour_directional_pool._target_candle_open_seconds(self.now) * 1000
        latest = target - (duration if row.get("stale") else 0)
        candles = []
        for index in range(65):
            candles.append(
                {
                    "time": latest - ((64 - index) * duration),
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

    @staticmethod
    def assert_interval(interval):
        if interval != "240":
            raise AssertionError(f"unexpected interval {interval}")


class FourHourDirectionalPoolStep3Tests(unittest.TestCase):
    def setUp(self):
        four_hour_directional_pool._reset_for_tests()

    def tearDown(self):
        four_hour_directional_pool._reset_for_tests()

    @staticmethod
    def classifier(candles):
        last = candles[-1]
        return last["trend"], float(last["score"]), "test 4H trend"

    def test_selects_global_top_50_without_forced_direction_quota(self):
        now = int(datetime(2026, 8, 3, 8, 5, tzinfo=timezone.utc).timestamp())
        symbols = [f"COIN{index:03d}USDT" for index in range(60)]
        daily_rows = []
        rows = {}
        for index, symbol in enumerate(symbols):
            direction = "BULLISH" if index < 55 else "BEARISH"
            score = 100 - index
            rows[symbol] = {"trend": direction, "score": score}
            daily_rows.append(
                {
                    "symbol": symbol,
                    "dailyTrend": "BULLISH",
                    "dailyTrendScore": 70 + index / 10,
                    "lastPrice": 1 + index,
                    "turnover24h": 1_000_000 + index,
                    "spreadPct": 0.05,
                }
            )
        daily_payload = {"status": "ready", "symbols": symbols, "rows": daily_rows}
        base_calls = {"count": 0}

        class Worker:
            @staticmethod
            def _fetch_active_usdt_symbols(core):
                base_calls["count"] += 1
                return list(symbols), {
                    symbol: {
                        "lastPrice": 1.0,
                        "turnover24h": 1_000_000,
                        "spreadPct": 0.05,
                    }
                    for symbol in symbols
                }

            classify_trend = staticmethod(self.classifier)

        core = FakeCore(rows, daily_payload, now)
        with (
            patch.dict(os.environ, {"FOUR_HOUR_DIRECTIONAL_POOL_SIZE": "50"}, clear=False),
            patch("backend.four_hour_directional_pool.time.time", return_value=now),
        ):
            four_hour_directional_pool.install(core, Worker)
            result = four_hour_directional_pool.build(core, now=now)
            selected, selected_tickers = Worker._fetch_active_usdt_symbols(core)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["dailyUniverseInput"], 60)
        self.assertEqual(result["metrics"]["directionalQualified"], 60)
        self.assertEqual(result["metrics"]["selected"], 50)
        self.assertEqual(result["metrics"]["bullish"], 50)
        self.assertEqual(result["metrics"]["bearish"], 0)
        self.assertFalse(result["metrics"]["forcedDirectionQuota"])
        self.assertEqual(result["symbols"], symbols[:50])
        self.assertEqual(selected, result["symbols"])
        self.assertEqual(set(selected_tickers), set(result["symbols"]))
        self.assertEqual(base_calls["count"], 0)
        self.assertTrue(result["persisted"])
        persisted = core._durable_state_store.get("four_hour_directional_pool_v1")
        self.assertEqual(persisted["symbols"], result["symbols"])

    def test_direction_change_is_classified_but_neutral_and_stale_are_rejected(self):
        now = int(datetime(2026, 8, 3, 12, 5, tzinfo=timezone.utc).timestamp())
        symbols = ["REVERSEUSDT", "NEUTRALUSDT", "STALEUSDT"]
        daily_rows = [
            {
                "symbol": symbol,
                "dailyTrend": "BULLISH",
                "dailyTrendScore": 80,
                "lastPrice": 1,
                "turnover24h": 2_000_000,
                "spreadPct": 0.04,
            }
            for symbol in symbols
        ]
        rows = {
            "REVERSEUSDT": {"trend": "BEARISH", "score": 95},
            "NEUTRALUSDT": {"trend": None, "score": 0},
            "STALEUSDT": {"trend": "BULLISH", "score": 90, "stale": True},
        }
        core = FakeCore(rows, {"symbols": symbols, "rows": daily_rows}, now)

        class Worker:
            @staticmethod
            def _fetch_active_usdt_symbols(core):
                return list(symbols), {
                    symbol: {"lastPrice": 1, "turnover24h": 2_000_000, "spreadPct": 0.04}
                    for symbol in symbols
                }

            classify_trend = staticmethod(self.classifier)

        four_hour_directional_pool.install(core, Worker)
        result = four_hour_directional_pool.build(core, now=now)

        self.assertEqual(result["symbols"], ["REVERSEUSDT"])
        self.assertEqual(result["rows"][0]["direction"], "BEARISH")
        self.assertTrue(result["rows"][0]["directionChangedFromDaily"])
        self.assertEqual(result["metrics"]["rejected"]["neutralOrUnclear"], 1)
        self.assertEqual(result["metrics"]["rejected"]["stale4hCandle"], 1)

    def test_empty_pool_is_persisted_once_and_worker_falls_back(self):
        now = int(datetime(2026, 8, 3, 16, 5, tzinfo=timezone.utc).timestamp())
        symbols = ["WAIT1USDT", "WAIT2USDT"]
        daily_rows = [
            {
                "symbol": symbol,
                "dailyTrend": "BULLISH",
                "dailyTrendScore": 80,
                "lastPrice": 1,
                "turnover24h": 1_000_000,
                "spreadPct": 0.05,
            }
            for symbol in symbols
        ]
        rows = {symbol: {"trend": None, "score": 0} for symbol in symbols}
        calls = {"base": 0}

        class Worker:
            @staticmethod
            def _fetch_active_usdt_symbols(core):
                calls["base"] += 1
                return list(symbols), {
                    symbol: {"lastPrice": 1, "turnover24h": 1_000_000, "spreadPct": 0.05}
                    for symbol in symbols
                }

            classify_trend = staticmethod(self.classifier)

        core = FakeCore(rows, {"symbols": symbols, "rows": daily_rows}, now)
        with patch("backend.four_hour_directional_pool.time.time", return_value=now):
            four_hour_directional_pool.install(core, Worker)
            result = four_hour_directional_pool.build(core, now=now)
            fallback_symbols, fallback_tickers = Worker._fetch_active_usdt_symbols(core)

        self.assertEqual(result["status"], "empty")
        self.assertTrue(result["persisted"])
        self.assertFalse(four_hour_directional_pool.due(now))
        self.assertEqual(fallback_symbols, symbols)
        self.assertEqual(set(fallback_tickers), set(symbols))
        self.assertEqual(calls["base"], 1)

    def test_target_changes_only_when_a_new_4h_candle_closes(self):
        before = int(datetime(2026, 8, 3, 7, 59, tzinfo=timezone.utc).timestamp())
        after = int(datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            four_hour_directional_pool._target_candle_open_seconds(before),
            int(datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(
            four_hour_directional_pool._target_candle_open_seconds(after),
            int(datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc).timestamp()),
        )


if __name__ == "__main__":
    unittest.main()
