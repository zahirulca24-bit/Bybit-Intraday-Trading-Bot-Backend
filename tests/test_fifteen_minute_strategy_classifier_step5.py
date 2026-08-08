from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from backend import fifteen_minute_strategy_classifier


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {"ok": True, "degraded": False, "persistentPathConfigured": True}

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def put(self, key, value):
        self.values[key] = copy.deepcopy(value)


class FakeSetupWorker:
    queue_writes = 0

    @staticmethod
    def settings():
        return {"minimumClosedCandles": 60}

    @staticmethod
    def _expected_side(trend):
        if trend == "BULLISH":
            return "Buy"
        if trend == "BEARISH":
            return "Sell"
        return None

    @staticmethod
    def _actionable_vote(votes, expected_side):
        aligned = [vote for vote in votes if vote.get("signal") == expected_side]
        if not aligned:
            return None
        return max(aligned, key=lambda vote: abs(float(vote.get("strength") or 0)))

    @staticmethod
    def _queue_candidate(candidate, queue_limit):
        FakeSetupWorker.queue_writes += 1
        return True


class FakeCore:
    def __init__(self, rows, now):
        self._durable_state_store = MemoryStore()
        self.rows = copy.deepcopy(rows)
        self.now = now
        self.evaluate_calls = []
        self.fetch_calls = []
        self.engine_calls = 0

    def hourly_watchlist(self, force=False):
        return {
            "status": "ready",
            "symbols": [row["symbol"] for row in self.rows],
            "rows": copy.deepcopy(self.rows),
        }

    @staticmethod
    def simple_atr(highs, lows, closes, period):
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return None
        true_ranges = []
        for index in range(1, len(closes)):
            true_ranges.append(
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - closes[index - 1]),
                    abs(lows[index] - closes[index - 1]),
                )
            )
        sample = true_ranges[-period:]
        return sum(sample) / len(sample) if sample else None

    def fetch_candles(self, symbol, interval, limit=80):
        self.fetch_calls.append((symbol, interval, limit))
        if interval != "15":
            raise AssertionError(f"unexpected direct history interval {interval}")
        source = next(row for row in self.rows if row["symbol"] == symbol)
        if source.get("missing15m"):
            return [], "missing"
        duration = 15 * 60 * 1000
        target = fifteen_minute_strategy_classifier._target_candle_open_seconds(self.now) * 1000
        latest = target - (duration if source.get("stale15m") else 0)
        candles = []
        for index in range(65):
            candles.append(
                {
                    "time": latest - ((64 - index) * duration),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                }
            )
        return candles[-limit:], "OK"

    def evaluate_signal(self, symbol, interval, mode):
        self.engine_calls += 1
        self.evaluate_calls.append((symbol, interval, mode))
        source = next(row for row in self.rows if row["symbol"] == symbol)
        if source.get("engineError"):
            raise RuntimeError("engine unavailable")

        expected = "Buy" if source.get("oneHourTrend") == "BULLISH" else "Sell"
        if source.get("waiting"):
            votes = [
                {
                    "engine": "Trend Follow",
                    "signal": "WAIT",
                    "strength": 0,
                    "reason": "Trigger not ready",
                },
                {
                    "engine": "S/R Breakout",
                    "signal": "WAIT",
                    "strength": 0,
                    "reason": "Breakout not ready",
                },
            ]
            signal = "WAIT"
        else:
            votes = [
                {
                    "engine": "Trend Follow",
                    "signal": expected,
                    "strength": 4.6,
                    "reason": "Existing strategy matched",
                },
                {
                    "engine": "S/R Breakout",
                    "signal": "WAIT",
                    "strength": 0,
                    "reason": "Breakout not ready",
                },
            ]
            signal = expected

        entry_interval = "15" if source.get("invalid5m") else "5"
        candle_time = None if source.get("missing5mIdentity") else (
            fifteen_minute_strategy_classifier._target_candle_open_seconds(self.now)
            * 1000
        )
        return (
            signal,
            "existing router result",
            votes,
            {"decision": signal, "mode": mode},
            {"entryInterval": entry_interval, "signalCandleTime": candle_time},
            {"strategy": "ok", "router": "ok"},
        )


class FifteenMinuteStrategyClassifierStep5Tests(unittest.TestCase):
    def setUp(self):
        fifteen_minute_strategy_classifier._reset_for_tests()
        FakeSetupWorker.queue_writes = 0

    def tearDown(self):
        fifteen_minute_strategy_classifier._reset_for_tests()
        FakeSetupWorker.queue_writes = 0

    @staticmethod
    def _row(symbol, trend="BULLISH", **extra):
        return {
            "symbol": symbol,
            "trend": trend,
            "oneHourTrend": trend,
            "oneHourCandleTime": 1,
            **extra,
        }

    def test_processes_top20_with_real_5m_context_without_queue_or_order_side_effect(self):
        now = int(datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc).timestamp())
        rows = [self._row(f"COIN{index:02d}USDT") for index in range(20)]
        core = FakeCore(rows, now)

        original_queue = FakeSetupWorker._queue_candidate
        fifteen_minute_strategy_classifier.install(core, FakeSetupWorker)
        result = fifteen_minute_strategy_classifier.build(core, now=now)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metrics"]["hourlyWatchlistInput"], 20)
        self.assertEqual(result["metrics"]["processed"], 20)
        self.assertEqual(result["metrics"]["setupClassified"], 20)
        self.assertEqual(result["metrics"]["entryQueueWrites"], 0)
        self.assertEqual(result["metrics"]["orderSubmissions"], 0)
        self.assertEqual(FakeSetupWorker.queue_writes, 0)
        self.assertIs(FakeSetupWorker._queue_candidate, original_queue)
        self.assertTrue(result["persisted"])
        self.assertFalse(fifteen_minute_strategy_classifier.due(now))
        self.assertEqual(len(core.evaluate_calls), 20)
        self.assertTrue(
            all(interval == "5" and mode == "aggressive" for _, interval, mode in core.evaluate_calls)
        )
        self.assertTrue(all(row["usesRealFiveMinuteContext"] for row in result["rows"]))
        self.assertTrue(all(row["entryEligible"] is False for row in result["rows"]))
        self.assertTrue(all(row["queued"] is False for row in result["rows"]))
        self.assertTrue(all(row["grade"] == "A+" for row in result["rows"]))

        previous_calls = core.engine_calls
        cached = fifteen_minute_strategy_classifier.ensure_current(core, now=now)
        self.assertEqual(cached["fifteenMinuteCandleTime"], result["fifteenMinuteCandleTime"])
        self.assertEqual(core.engine_calls, previous_calls)

        persisted = core._durable_state_store.get(
            "fifteen_minute_strategy_classification_v1"
        )
        self.assertEqual(persisted["symbols"], result["symbols"])

    def test_rejects_stale_missing_invalid_context_and_engine_errors(self):
        now = int(datetime(2026, 8, 3, 12, 15, tzinfo=timezone.utc).timestamp())
        rows = [
            self._row("MISSINGUSDT", missing15m=True),
            self._row("STALEUSDT", stale15m=True),
            self._row("NEUTRALUSDT", trend="NEUTRAL"),
            self._row("BAD5MUSDT", invalid5m=True),
            self._row("ENGINEUSDT", engineError=True),
        ]
        core = FakeCore(rows, now)
        fifteen_minute_strategy_classifier.install(core, FakeSetupWorker)

        result = fifteen_minute_strategy_classifier.build(core, now=now)

        rejected = result["metrics"]["rejected"]
        self.assertEqual(rejected["missing15mHistory"], 1)
        self.assertEqual(rejected["stale15mCandle"], 1)
        self.assertEqual(rejected["unsupportedDirection"], 1)
        self.assertEqual(rejected["invalid5mContext"], 1)
        self.assertEqual(rejected["engineError"], 1)
        self.assertEqual(result["metrics"]["errors"], 2)
        self.assertEqual(result["metrics"]["noSetup"], 3)
        self.assertEqual(FakeSetupWorker.queue_writes, 0)

    def test_waiting_existing_strategies_remain_watching_not_confirmed(self):
        now = int(datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc).timestamp())
        core = FakeCore([self._row("WAITUSDT", waiting=True)], now)
        fifteen_minute_strategy_classifier.install(core, FakeSetupWorker)

        result = fifteen_minute_strategy_classifier.build(core, now=now)
        row = result["rows"][0]

        self.assertEqual(row["status"], "WATCHING")
        self.assertIsNone(row["strategy"])
        self.assertEqual(row["matchedStrategies"], [])
        self.assertEqual(
            row["waitingStrategies"], ["Trend Follow", "S/R Breakout"]
        )
        self.assertFalse(row["entryEligible"])
        self.assertFalse(row["queued"])

    def test_target_changes_only_after_a_new_15m_candle_closes(self):
        before = int(datetime(2026, 8, 3, 7, 59, tzinfo=timezone.utc).timestamp())
        after = int(datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            fifteen_minute_strategy_classifier._target_candle_open_seconds(before),
            int(datetime(2026, 8, 3, 7, 30, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(
            fifteen_minute_strategy_classifier._target_candle_open_seconds(after),
            int(datetime(2026, 8, 3, 7, 45, tzinfo=timezone.utc).timestamp()),
        )


if __name__ == "__main__":
    unittest.main()
