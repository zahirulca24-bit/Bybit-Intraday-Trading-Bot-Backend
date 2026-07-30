from backend import scanner_execution_gate


class FakeEngine:
    def __init__(self):
        self.status = {}

    def set_status(self, name, value):
        self.status[name] = value


class FakeCore:
    """Deliberately has no normalize_interval attribute, matching backend.server."""

    def __init__(self):
        self.calls = []

    def fetch_candles(self, symbol, interval, limit=120):
        self.calls.append((symbol, interval, limit))
        duration = {"5": 300_000, "15": 900_000, "60": 3_600_000}[interval]
        rows = [
            {
                "time": i * duration,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
            }
            for i in range(60)
        ]
        return rows, "OK"


def test_closed_market_snapshot_uses_guarded_normalizer_and_closed_entry_timestamp():
    core = FakeCore()
    engine = FakeEngine()

    snapshot = scanner_execution_gate.closed_market_snapshot(core, engine, "BTCUSDT", "5")

    assert snapshot["ok"] is True
    assert snapshot["entryInterval"] == "5"
    assert snapshot["signalCandleTime"] == 59 * 300_000
    assert snapshot["timeframes"]["5M"][-1]["time"] == snapshot["signalCandleTime"]
    assert engine.status["marketData"] == "ok"
    assert core.calls == [
        ("BTCUSDT", "60", 120),
        ("BTCUSDT", "15", 120),
        ("BTCUSDT", "5", 120),
    ]


def test_closed_market_snapshot_fails_without_entry_history():
    core = FakeCore()
    original = core.fetch_candles

    def missing_entry(symbol, interval, limit=120):
        if interval == "5":
            return None, "Not enough closed candles"
        return original(symbol, interval, limit)

    core.fetch_candles = missing_entry
    engine = FakeEngine()

    snapshot = scanner_execution_gate.closed_market_snapshot(core, engine, "BTCUSDT", "5")

    assert snapshot["ok"] is False
    assert snapshot["signalCandleTime"] is None
    assert engine.status["marketData"] == "error"
