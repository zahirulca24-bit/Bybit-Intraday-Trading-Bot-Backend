import pytest

from backend import five_minute_entry_confirmation as confirmation


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {"ok": True, "degraded": False}

    def get(self, key):
        value = self.values.get(key)
        if isinstance(value, dict):
            return dict(value)
        return value

    def put(self, key, value):
        self.values[key] = dict(value)
        return True


class SetupWorkerStub:
    @staticmethod
    def settings():
        return {"queueLimit": 100}


class CoreStub:
    def __init__(self, *, vote_signal="Buy", vote_strength=4.8):
        self._durable_state_store = MemoryStore()
        self.vote_signal = vote_signal
        self.vote_strength = vote_strength
        self.evaluate_calls = []
        self.fetch_calls = []
        self.classification = classification_payload()

    def fifteen_minute_strategy_classification_status(self):
        return dict(self.classification)

    def fetch_candles(self, symbol, interval, limit=120):
        self.fetch_calls.append((symbol, interval, limit))
        assert interval == "5"
        target = 3_600_000
        rows = []
        for index in range(60):
            candle_time = target - ((59 - index) * 300_000)
            price = 100 + index * 0.01
            rows.append(
                {
                    "time": candle_time,
                    "open": price - 0.02,
                    "high": price + 0.05,
                    "low": price - 0.05,
                    "close": price,
                    "volume": 1000 + index,
                }
            )
        return rows, "OK"

    def evaluate_signal(self, symbol, interval, mode):
        self.evaluate_calls.append((symbol, interval, mode))
        return (
            self.vote_signal if self.vote_signal in {"Buy", "Sell"} else "WAIT",
            "existing strategy evaluation",
            [
                {
                    "engine": "Trend Follow",
                    "signal": self.vote_signal,
                    "strength": self.vote_strength,
                    "reason": "existing 5M trigger result",
                },
                {
                    "engine": "S/R Breakout",
                    "signal": "WAIT",
                    "strength": 0,
                    "reason": "waiting",
                },
            ],
            {"decision": self.vote_signal, "mode": mode},
            {
                "entryInterval": "5",
                "signalCandleTime": 3_600_000,
            },
            {"strategy": "ok"},
        )


def classification_payload():
    return {
        "status": "ready",
        "fifteenMinuteCandleTime": 2_700_000,
        "rows": [
            {
                "status": "SETUP_CLASSIFIED",
                "symbol": "BTCUSDT",
                "expectedSide": "Buy",
                "strategy": "Trend Follow",
                "strategyReason": "existing 15M setup",
                "strategyStrength": 4.8,
                "grade": "A+",
                "gradeScore": 96.0,
                "gradeExecutionEligible": True,
            }
        ],
    }


@pytest.fixture(autouse=True)
def reset_confirmation():
    confirmation._reset_for_tests()
    yield
    confirmation._reset_for_tests()


def install(core):
    confirmation.install(core, SetupWorkerStub())


def test_same_strategy_and_side_confirm_exact_closed_5m_entry():
    core = CoreStub()
    install(core)

    result = confirmation.build(core, now=3900)

    assert result["status"] == "ready"
    assert result["fiveMinuteCandleTime"] == 3_600_000
    assert result["setupFifteenMinuteCandleTime"] == 2_700_000
    assert result["metrics"]["confirmed"] == 1
    assert result["metrics"]["queuedNow"] == 1
    assert result["riskChecks"] == 0
    assert result["positionSizingCalls"] == 0
    assert result["orderSubmissions"] == 0
    assert result["confirmedEntryQueueSize"] == 1

    candidate = result["confirmedEntryQueue"][0]
    assert candidate["symbol"] == "BTCUSDT"
    assert candidate["side"] == "Buy"
    assert candidate["strategy"] == "Trend Follow"
    assert candidate["entryFiveMinuteCandleTime"] == 3_600_000
    assert candidate["riskStatus"] == "PENDING_RISK"
    assert candidate["positionSizingStatus"] == "NOT_EVALUATED_STEP8"
    assert candidate["executionStatus"] == "AWAITING_NODE_EXECUTION"
    assert candidate["orderSubmitted"] is False
    assert core.evaluate_calls == [("BTCUSDT", "5", "aggressive")]


def test_waiting_same_strategy_does_not_create_candidate():
    core = CoreStub(vote_signal="WAIT", vote_strength=0)
    install(core)

    result = confirmation.build(core, now=3900)

    assert result["rows"][0]["status"] == "ENTRY_WAIT"
    assert result["confirmedEntryQueueSize"] == 0
    assert result["metrics"]["confirmed"] == 0


def test_opposing_same_strategy_invalidates_setup():
    core = CoreStub(vote_signal="Sell", vote_strength=4.8)
    install(core)

    result = confirmation.build(core, now=3900)

    assert result["rows"][0]["status"] == "SETUP_INVALIDATED"
    assert result["metrics"]["invalidated"] == 1
    assert result["confirmedEntryQueueSize"] == 0


def test_existing_grade_threshold_blocks_b_plus_entry():
    core = CoreStub(vote_signal="Buy", vote_strength=3.3)
    install(core)

    result = confirmation.build(core, now=3900)

    assert result["rows"][0]["status"] == "BLOCKED_GRADE"
    assert result["rows"][0]["entryGrade"] == "B+"
    assert result["confirmedEntryQueueSize"] == 0


def test_stale_15m_classification_fails_closed():
    core = CoreStub()
    core.classification["fifteenMinuteCandleTime"] = 1_800_000
    install(core)

    result = confirmation.build(core, now=3900)

    assert result["status"] == "error"
    assert "stale" in result["lastError"].lower()
    assert core.evaluate_calls == []


def test_new_15m_setup_waits_for_later_closed_5m_candle():
    core = CoreStub()
    core.classification = {
        **classification_payload(),
        "fifteenMinuteCandleTime": 3_600_000,
    }
    install(core)

    result = confirmation.build(core, now=4500)

    assert result["status"] == "waiting"
    assert result["metrics"]["confirmed"] == 0
    assert "Awaiting the first closed 5M" in result["metrics"]["reason"]
    assert core.evaluate_calls == []


def test_same_closed_5m_candle_is_not_processed_twice():
    core = CoreStub()
    install(core)

    first = confirmation.ensure_current(core, now=3900)
    second = confirmation.ensure_current(core, now=3950)

    assert first["confirmedEntryQueueSize"] == 1
    assert second["confirmedEntryQueueSize"] == 1
    assert core.evaluate_calls == [("BTCUSDT", "5", "aggressive")]


def test_confirmation_snapshot_is_persisted_and_reloaded():
    core = CoreStub()
    store = core._durable_state_store
    install(core)
    result = confirmation.build(core, now=3900)
    assert result["persisted"] is True

    confirmation._reset_for_tests()
    restarted_core = CoreStub()
    restarted_core._durable_state_store = store
    confirmation.install(restarted_core, SetupWorkerStub())
    restored = confirmation.snapshot()

    assert restored["fiveMinuteCandleTime"] == 3_600_000
    assert restored["confirmedEntryQueueSize"] == 1
    assert restored["confirmedEntryQueue"][0]["candidateKey"] == result[
        "confirmedEntryQueue"
    ][0]["candidateKey"]
