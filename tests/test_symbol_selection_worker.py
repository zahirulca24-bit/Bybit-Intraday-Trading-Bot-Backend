from backend import worker


class FakeCore:
    def __init__(self, symbols):
        self.symbols = symbols

    def public_bybit_get(self, path, params):
        assert path == "/v5/market/tickers"
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": symbol,
                        "lastPrice": "100",
                        "bid1Price": "99.95",
                        "ask1Price": "100.05",
                        "turnover24h": "50000000",
                    }
                    for symbol in self.symbols
                ]
            },
        }

    def fetch_candles(self, symbol, interval, limit=80):
        assert interval == "60"
        sequence = int(symbol.removeprefix("S").removesuffix("USDT"))
        bullish = sequence % 2 == 0
        closes = [100 + index * 0.5 for index in range(80)]
        if not bullish:
            closes = list(reversed(closes))
        return [
            {
                "time": index,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000,
            }
            for index, close in enumerate(closes)
        ], "OK"


def reset_state():
    worker._STATE.update(
        {
            "status": "idle",
            "allSymbols": [],
            "currentIndex": 0,
            "batchSize": 100,
            "cycleNumber": 0,
            "scannedInCycle": 0,
            "candidates": {},
            "activeSymbols": [],
            "rows": [],
            "updatedAt": 0,
            "lastBatchAt": 0,
            "lastFullCycleAt": 0,
            "lastError": None,
            "lastBatch": {},
        }
    )


def test_trend_classifier_returns_only_bullish_or_bearish():
    rising = [{"close": 100 + index} for index in range(80)]
    falling = [{"close": 180 - index} for index in range(80)]

    assert worker.classify_trend(rising)[0] == "BULLISH"
    assert worker.classify_trend(falling)[0] == "BEARISH"


def test_worker_rotates_batches_and_keeps_maximum_30(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_BATCH_SIZE", "100")
    monkeypatch.setenv("SYMBOL_WORKER_ACTIVE_POOL_SIZE", "30")
    symbols = [f"S{index}USDT" for index in range(1, 221)]
    core = FakeCore(symbols)

    first = worker.run_batch(core, now=1000)
    second = worker.run_batch(core, now=2000)
    third = worker.run_batch(core, now=3000)

    assert first["lastBatch"]["requested"] == 100
    assert first["currentIndex"] == 100
    assert second["lastBatch"]["requested"] == 100
    assert second["currentIndex"] == 200
    assert third["lastBatch"]["requested"] == 20
    assert third["lastBatch"]["wrapped"] is True
    assert third["currentIndex"] == 0
    assert third["cycleNumber"] == 1
    assert len(third["activeSymbols"]) == 30
    assert {row["trend"] for row in third["rows"]} <= {"BULLISH", "BEARISH"}
