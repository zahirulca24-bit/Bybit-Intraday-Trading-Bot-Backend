from backend import strategy_step3_upgrade as step3


def _candles(count=60, start=100.0, step=0.5, span=1.0):
    rows = []
    price = start
    for index in range(count):
        close = price + step
        rows.append({
            "time": index * 3_600_000,
            "open": price,
            "high": max(price, close) + span / 2,
            "low": min(price, close) - span / 2,
            "close": close,
            "volume": 1000,
        })
        price = close
    return rows


def test_trending_regime_detects_direction():
    regime = step3.detect_regime(_candles(step=0.8))
    assert regime["regime"] == "TRENDING"
    assert regime["direction"] == "Buy"


def test_ranging_regime_blocks_trend_engine():
    candles = _candles(step=0.001, span=0.1)
    regime = step3.detect_regime(candles)
    votes = [{"engine": "ORB", "signal": "Buy", "strength": 4, "reason": "breakout"}]
    filtered = step3.filter_votes(votes, regime)
    assert regime["regime"] == "RANGING"
    assert filtered[0]["signal"] == "WAIT"
    assert filtered[0]["strength"] == 0


def test_opposing_vote_is_blocked_in_trend():
    regime = step3.detect_regime(_candles(step=0.8))
    votes = [{"engine": "Liquidity Sweep", "signal": "Sell", "strength": 4, "reason": "sweep"}]
    filtered = step3.filter_votes(votes, regime)
    assert filtered[0]["signal"] == "WAIT"
    assert filtered[0]["marketRegime"]["direction"] == "Buy"


def test_trade_quality_groups_attributed_rows_and_keeps_truthful_unattributed_count():
    rows = [
        {"closedPnl": 10, "strategy": "ORB", "grade": "A+", "session": "LONDON", "marketRegime": "TRENDING", "realizedR": 2.0},
        {"closedPnl": -5, "strategy": "ORB", "grade": "A", "session": "LONDON", "marketRegime": "TRENDING", "realizedR": -1.0},
        {"closedPnl": 3},
    ]
    quality = step3.trade_quality_snapshot(rows)
    assert quality["sampleSize"] == 3
    assert quality["attributedTrades"] == 2
    assert quality["unattributedTrades"] == 1
    orb = quality["byStrategy"][0]
    assert orb["label"] == "ORB"
    assert orb["totalTrades"] == 2
    assert orb["winRatePct"] == 50.0
    assert orb["averageR"] == 0.5
