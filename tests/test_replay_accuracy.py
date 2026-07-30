from backend import replay_accuracy


def candle(open_time, price=100.0, volume=100.0):
    return {
        "time": open_time,
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
        "volume": volume,
    }


def test_closed_at_excludes_incomplete_higher_timeframe_candle():
    rows = [candle(0), candle(900_000), candle(1_800_000)]
    assert [row["time"] for row in replay_accuracy.closed_at(rows, 1_800_000, "15")] == [0, 900_000]


def test_closed_at_orders_and_bounds_rows():
    rows = [candle(900_000), candle(0), candle(1_800_000)]
    selected = replay_accuracy.closed_at(rows, 2_700_000, "15", limit=2)
    assert [row["time"] for row in selected] == [900_000, 1_800_000]


class FakeCore:
    def __init__(self):
        self.series = {}

    def fetch_candles(self, symbol, interval, limit=120):
        return list(self.series[interval])[-limit:], "OK"

    @staticmethod
    def vote(engine, signal, reason, strength=0):
        return {"engine": engine, "signal": signal, "reason": reason, "strength": strength}

    @staticmethod
    def avg_volume(rows, period):
        window = rows[-period:]
        return sum(row["volume"] for row in window) / len(window)

    @staticmethod
    def trend_following_engine(*args):
        return FakeCore.vote("Trend Follow", "Buy", "fixture")

    @staticmethod
    def sr_breakout_engine(*args):
        return FakeCore.vote("S/R Breakout", "WAIT", "fixture")

    @staticmethod
    def rsi_divergence_engine(*args):
        return FakeCore.vote("RSI Divergence", "WAIT", "fixture")

    @staticmethod
    def vwap_bounce_engine(*args):
        return FakeCore.vote("VWAP Bounce", "WAIT", "fixture")

    @staticmethod
    def route_votes(votes, mode):
        return {"decision": "Buy", "confidence": 1, "requiredVotes": 1, "mode": mode, "reason": "fixture"}

    @staticmethod
    def normalize_mode(mode):
        return mode

    @staticmethod
    def estimate_trade_outcome(side, entry, future, stop, target):
        return "win", 1, future[0]["close"]


def test_replay_enters_at_next_candle_open_not_signal_close():
    core = FakeCore()
    base = 1_700_000_000_000
    core.series["5"] = [candle(base + i * 300_000, 100 + i * 0.01) for i in range(130)]
    core.series["15"] = [candle(base - 60 * 900_000 + i * 900_000, 100) for i in range(130)]
    core.series["60"] = [candle(base - 60 * 3_600_000 + i * 3_600_000, 100) for i in range(130)]
    result = replay_accuracy.replay(core, "BTCUSDT", "24h", "balanced", 0.8, 2.0)
    assert result["ok"] is True
    assert result["methodology"]["entry"] == "next_candle_open"
    assert result["methodology"]["closedCandlesOnly"] is True
    if result["trades"]:
        trade = result["trades"][0]
        assert trade["entryTime"] >= trade["decisionTime"]


def test_install_sets_live_supported_take_profit_default():
    class Core:
        pass

    core = Core()
    replay_accuracy.install(core)
    assert core._accurate_replay_installed is True
    assert core.replay_strategy_quality.__defaults__[-1] == 2.0
