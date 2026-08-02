from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend import hourly_watchlist


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
    def __init__(self, rows_by_symbol, four_hour_payload, now):
        self._durable_state_store = MemoryStore()
        self.rows_by_symbol = rows_by_symbol
        self.four_hour_payload = four_hour_payload
        self.now = now
        self.fetch_count = 0

    def four_hour_directional_pool(self, force=False):
        return copy.deepcopy(self.four_hour_payload)

    def fetch_candles(self, symbol, interval, limit=80):
        if interval != "60":
            raise AssertionError(f"unexpected interval {interval}")
        self.fetch_count += 1
        row = self.rows_by_symbol[symbol]
        if row.get("missing"):
            return [], "missing"

        duration = 3_600_000
        target = hourly_watchlist._target_candle_open_seconds(self.now) * 1000
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


class HourlyWatchlistStep4Tests(unittest.TestCase):
    def setUp(self):
        hourly_watchlist._reset_for_tests()

    def tearDown(self):
        hourly_watchlist._reset_for_tests()

    @staticmethod
    def classifier(candles):
        last = candles[-1]
        return last["trend"], float(last["score"]), "existing worker trend"

    def test_selects_top_20_with_existing_ranker_and_persists(self):
        now = int(datetime(2026, 8, 3, 10, 5, tzinfo=timezone.utc).timestamp())
        symbols = [f"COIN{index:03d}USDT" for index in range(25)]
        rows_by_symbol = {}
        upstream_rows = []
        for index, symbol in enumerate(symbols):
            one_hour_trend = "BEARISH" if index < 5 else "BULLISH"
            rows_by_symbol[symbol] = {
                "trend": one_hour_trend,
                "score": 100 - index,
            }
            upstream_rows.append(
                {
                    "symbol": symbol,
                    "direction": "BULLISH",
                    "fourHourTrend": "BULLISH",
                    "fourHourTrendScore": 80,
                    "lastPrice": 1 + index,
                    "turnover24h": 10_000_000 + index,
                    "spreadPct": 0.05,
                }
            )

        rank_calls = {"count": 0}
        base_calls = {"count": 0}

        class Worker:
            @staticmethod
            def classify_trend(candles):
                return HourlyWatchlistStep4Tests.classifier(candles)

            @staticmethod
            def _rank_rows(rows):
                rank_calls["count"] += 1
                for row in rows:
                    row["rankScore"] = float(row["trendScore"])
                return sorted(rows, key=lambda row: row["rankScore"], reverse=True)

            @staticmethod
            def run_batch(core, now=None):
                base_calls["count"] += 1
                return {"status": "base"}

            @staticmethod
            def snapshot():
                return {"status": "base"}

        core = FakeCore(
            rows_by_symbol,
            {"status": "ready", "symbols": symbols, "rows": upstream_rows},
            now,
        )
        with (
            patch.dict(os.environ, {"HOURLY_WATCHLIST_SIZE": "20"}, clear=False),
            patch("backend.hourly_watchlist.time.time", return_value=now),
        ):
            hourly_watchlist.install(core, Worker)
            result = Worker.run_batch(core, now=now)
            worker_snapshot = Worker.snapshot()
            fetches_after_first_run = core.fetch_count
            same_candle = Worker.run_batch(core, now=now + 300)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["fourHourPoolInput"], 25)
        self.assertEqual(result["metrics"]["oneHourQualified"], 25)
        self.assertEqual(result["metrics"]["selected"], 20)
        self.assertEqual(result["symbols"], symbols[:20])
        self.assertEqual(result["metrics"]["directionChangedFrom4h"], 5)
        self.assertFalse(result["metrics"]["alignmentRequired"])
        self.assertEqual(result["metrics"]["rankingPolicy"], "existing_worker_rank_rows")
        self.assertEqual(rank_calls["count"], 1)
        self.assertEqual(base_calls["count"], 0)
        self.assertEqual(worker_snapshot["activeSymbols"], symbols[:20])
        self.assertEqual(same_candle["symbols"], symbols[:20])
        self.assertEqual(core.fetch_count, fetches_after_first_run)
        self.assertTrue(result["persisted"])
        persisted = core._durable_state_store.get("hourly_watchlist_top20_v1")
        self.assertEqual(persisted["symbols"], symbols[:20])

    def test_neutral_stale_and_missing_history_are_rejected(self):
        now = int(datetime(2026, 8, 3, 11, 5, tzinfo=timezone.utc).timestamp())
        symbols = ["GOODUSDT", "NEUTRALUSDT", "STALEUSDT", "MISSINGUSDT"]
        upstream_rows = [
            {
                "symbol": symbol,
                "direction": "BULLISH",
                "fourHourTrend": "BULLISH",
                "fourHourTrendScore": 80,
                "lastPrice": 1,
                "turnover24h": 10_000_000,
                "spreadPct": 0.05,
            }
            for symbol in symbols
        ]
        rows_by_symbol = {
            "GOODUSDT": {"trend": "BULLISH", "score": 90},
            "NEUTRALUSDT": {"trend": None, "score": 0},
            "STALEUSDT": {"trend": "BULLISH", "score": 85, "stale": True},
            "MISSINGUSDT": {"trend": "BULLISH", "score": 80, "missing": True},
        }

        class Worker:
            classify_trend = staticmethod(self.classifier)

            @staticmethod
            def _rank_rows(rows):
                return sorted(rows, key=lambda row: row["trendScore"], reverse=True)

            @staticmethod
            def run_batch(core, now=None):
                return {"status": "base"}

            @staticmethod
            def snapshot():
                return {"status": "base"}

        core = FakeCore(
            rows_by_symbol,
            {"status": "ready", "symbols": symbols, "rows": upstream_rows},
            now,
        )
        hourly_watchlist.install(core, Worker)
        result = hourly_watchlist.build(core, now=now)

        self.assertEqual(result["symbols"], ["GOODUSDT"])
        self.assertEqual(result["metrics"]["rejected"]["neutralOrUnclear"], 1)
        self.assertEqual(result["metrics"]["rejected"]["stale1hCandle"], 1)
        self.assertEqual(result["metrics"]["rejected"]["missing1hHistory"], 1)

    def test_empty_result_is_persisted_and_not_rebuilt_in_same_hour(self):
        now = int(datetime(2026, 8, 3, 12, 5, tzinfo=timezone.utc).timestamp())
        symbols = ["WAIT1USDT", "WAIT2USDT"]
        upstream_rows = [
            {
                "symbol": symbol,
                "direction": "BULLISH",
                "fourHourTrend": "BULLISH",
                "fourHourTrendScore": 70,
                "lastPrice": 1,
                "turnover24h": 10_000_000,
                "spreadPct": 0.05,
            }
            for symbol in symbols
        ]
        rows_by_symbol = {
            symbol: {"trend": None, "score": 0} for symbol in symbols
        }

        class Worker:
            classify_trend = staticmethod(self.classifier)

            @staticmethod
            def _rank_rows(rows):
                return rows

            @staticmethod
            def run_batch(core, now=None):
                return {"status": "base"}

            @staticmethod
            def snapshot():
                return {"status": "base"}

        core = FakeCore(
            rows_by_symbol,
            {"status": "ready", "symbols": symbols, "rows": upstream_rows},
            now,
        )
        with patch("backend.hourly_watchlist.time.time", return_value=now):
            hourly_watchlist.install(core, Worker)
            first = Worker.run_batch(core, now=now)
            fetches = core.fetch_count
            second = Worker.run_batch(core, now=now + 600)

        self.assertEqual(first["status"], "empty")
        self.assertTrue(first["persisted"])
        self.assertFalse(hourly_watchlist.due(now))
        self.assertEqual(second["status"], "empty")
        self.assertEqual(core.fetch_count, fetches)

    def test_target_changes_only_after_new_hour_closes(self):
        before = int(datetime(2026, 8, 3, 10, 59, tzinfo=timezone.utc).timestamp())
        after = int(datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            hourly_watchlist._target_candle_open_seconds(before),
            int(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(
            hourly_watchlist._target_candle_open_seconds(after),
            int(datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc).timestamp()),
        )


if __name__ == "__main__":
    unittest.main()
