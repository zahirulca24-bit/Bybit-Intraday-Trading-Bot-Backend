from backend import setup_worker


class SymbolWorkerStub:
    def __init__(self, rows):
        self.rows = rows

    def snapshot(self):
        return {"rows": self.rows, "updatedAt": 123}


class CoreStub:
    def __init__(self, candles_by_symbol, votes_by_symbol):
        self.candles_by_symbol = candles_by_symbol
        self.votes_by_symbol = votes_by_symbol
        self.evaluated = []

    def fetch_candles(self, symbol, interval, limit=120):
        assert interval == "15"
        return self.candles_by_symbol[symbol], "OK"

    def evaluate_signal(self, symbol, interval, mode):
        self.evaluated.append((symbol, interval, mode))
        votes = self.votes_by_symbol.get(symbol, [])
        signal = next((vote["signal"] for vote in votes if vote["signal"] in {"Buy", "Sell"}), "WAIT")
        return signal, "stub", votes, {"decision": signal}, {}, "ok"


def candles(count=80, start=1_700_000_000_000, step=900_000, bullish=True):
    rows = []
    price = 100.0
    for index in range(count):
        open_price = price
        close = price + (0.5 if bullish else -0.5)
        rows.append({
            "time": start + (index * step),
            "open": open_price,
            "high": max(open_price, close) + 1.0,
            "low": min(open_price, close) - 1.0,
            "close": close,
            "volume": 1000 + index,
        })
        price = close
    return rows


def reset_state():
    setup_worker._STATE.clear()
    setup_worker._STATE.update({
        "status": "idle",
        "currentIndex": 0,
        "batchSize": 10,
        "cycleNumber": 0,
        "lastRunAt": 0,
        "lastError": None,
        "lastEvaluatedCandle": {},
        "rows": [],
        "confirmedQueue": [],
    })


def test_rotates_ten_symbols_and_wraps_after_thirty(monkeypatch):
    reset_state()
    monkeypatch.setenv("SETUP_WORKER_BATCH_SIZE", "10")
    rows = [{"symbol": f"S{index}USDT", "trend": "BULLISH"} for index in range(30)]
    data = {row["symbol"]: candles() for row in rows}
    waits = {row["symbol"]: [{"engine": "Trend Follow", "signal": "WAIT", "reason": "waiting", "strength": 0}] for row in rows}
    core = CoreStub(data, waits)
    worker = SymbolWorkerStub(rows)
    now = int((data[rows[0]["symbol"]][-1]["time"] + 900_000) / 1000) + 1

    first = setup_worker.run_batch(core, worker, now=now)
    second = setup_worker.run_batch(core, worker, now=now + 300)
    third = setup_worker.run_batch(core, worker, now=now + 600)

    assert first["lastBatch"]["startIndex"] == 0
    assert second["lastBatch"]["startIndex"] == 10
    assert third["lastBatch"]["startIndex"] == 20
    assert third["lastBatch"]["wrapped"] is True
    assert third["currentIndex"] == 0
    assert third["cycleNumber"] == 1


def test_one_aligned_strategy_vote_confirms_and_queues_candidate(monkeypatch):
    reset_state()
    monkeypatch.setenv("SETUP_WORKER_BATCH_SIZE", "10")
    symbol = "BTCUSDT"
    series = candles(bullish=True)
    core = CoreStub(
        {symbol: series},
        {symbol: [
            {"engine": "Trend Follow", "signal": "Buy", "reason": "confirmed", "strength": 2},
            {"engine": "VWAP Bounce", "signal": "WAIT", "reason": "waiting", "strength": 0},
        ]},
    )
    worker = SymbolWorkerStub([{"symbol": symbol, "trend": "BULLISH"}])
    now = int((series[-1]["time"] + 900_000) / 1000) + 1

    result = setup_worker.run_batch(core, worker, now=now)
    row = result["rows"][0]

    assert row["status"] == "CONFIRMED"
    assert row["side"] == "Buy"
    assert row["strategy"] == "Trend Follow"
    assert row["riskReward"] >= 2.0
    assert result["confirmedQueueSize"] == 1
    assert result["confirmedQueue"][0]["executionStatus"] == "PENDING_HANDOFF"


def test_same_closed_candle_is_not_evaluated_twice():
    reset_state()
    symbol = "ETHUSDT"
    series = candles(bullish=False)
    core = CoreStub(
        {symbol: series},
        {symbol: [{"engine": "S/R Breakout", "signal": "Sell", "reason": "confirmed", "strength": 3}]},
    )
    worker = SymbolWorkerStub([{"symbol": symbol, "trend": "BEARISH"}])
    now = int((series[-1]["time"] + 900_000) / 1000) + 1

    first = setup_worker.run_batch(core, worker, now=now)
    second = setup_worker.run_batch(core, worker, now=now + 300)

    assert first["rows"][0]["status"] == "CONFIRMED"
    assert second["rows"][0]["status"] == "SKIPPED"
    assert len(core.evaluated) == 1
    assert second["confirmedQueueSize"] == 1


def test_opposite_strategy_vote_does_not_confirm():
    reset_state()
    symbol = "SOLUSDT"
    series = candles(bullish=True)
    core = CoreStub(
        {symbol: series},
        {symbol: [{"engine": "RSI Divergence", "signal": "Sell", "reason": "opposite", "strength": 5}]},
    )
    worker = SymbolWorkerStub([{"symbol": symbol, "trend": "BULLISH"}])
    now = int((series[-1]["time"] + 900_000) / 1000) + 1

    result = setup_worker.run_batch(core, worker, now=now)

    assert result["rows"][0]["status"] == "NO_SETUP"
    assert result["confirmedQueueSize"] == 0
