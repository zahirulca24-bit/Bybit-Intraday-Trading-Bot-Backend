import sys
import os
from datetime import datetime, timezone

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.market_data import MarketDataEngine
from engines.router import route_votes
from engines.strategies import liquidity_sweep_engine
from engines.strategies import orb_engine as modular_orb_engine
from engines.strategies import rsi_divergence_engine
from engines.strategies import sr_breakout_engine
from engines.strategies import trend_following_engine
from engines.strategies import vwap_bounce_engine
from server import orb_engine as legacy_orb_engine, fetch_candles

class DummyResponse:
    def __init__(self, data):
        self.data = data
    def read(self):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def test_market_data_snapshot_handles_none():
    engine = MarketDataEngine("https://api-demo.bybit.com")

    # Mock self.candles to return None/strings instead of crashing
    original_candles = engine.candles
    try:
        # Scenario where one of them returns None for message
        engine.candles = lambda symbol, interval: ([], None) if interval == "5" else ([], "OK")
        res = engine.snapshot("BTCUSDT")
        assert res["ok"] is False
        assert res["message"] == "OK; OK"

        # Scenario where all return None for message
        engine.candles = lambda symbol, interval: ([], None)
        res = engine.snapshot("BTCUSDT")
        assert res["ok"] is False
        assert res["message"] == ""
    finally:
        engine.candles = original_candles

def test_orb_engine_no_candles_today():
    # Setup test data with candles entirely before today
    now = datetime.now(timezone.utc)
    today_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_utc_midnight_ms = int(today_utc_midnight.timestamp() * 1000)

    # 1H candles all in the past day
    tf1h = [
        {"time": today_utc_midnight_ms - 3600000 * 2, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10},
        {"time": today_utc_midnight_ms - 3600000, "open": 105, "high": 115, "low": 95, "close": 100, "volume": 10},
    ]
    tf15m = [{"time": today_utc_midnight_ms, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10}]
    tf5m = [
        {"time": today_utc_midnight_ms, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10}
        for _ in range(21) # Enough candles for average volume calc
    ]

    # Both engines should return a WAIT vote
    res_modular = modular_orb_engine(tf1h, tf15m, tf5m)
    assert res_modular["signal"] == "WAIT"
    assert "No 1H candle found for current UTC day" in res_modular["reason"]

    res_legacy = legacy_orb_engine(tf1h, tf15m, tf5m)
    assert res_legacy["signal"] == "WAIT"
    assert "No 1H candle found for current UTC day" in res_legacy["reason"]

def test_orb_engine_valid_candle_today():
    now = datetime.now(timezone.utc)
    today_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_utc_midnight_ms = int(today_utc_midnight.timestamp() * 1000)

    # First candle of the current UTC day is at UTC midnight
    tf1h = [
        {"time": today_utc_midnight_ms - 3600000, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10},
        {"time": today_utc_midnight_ms, "open": 105, "high": 120, "low": 100, "close": 115, "volume": 10}, # Opening range high: 120, low: 100
        {"time": today_utc_midnight_ms + 3600000, "open": 115, "high": 125, "low": 110, "close": 122, "volume": 10},
    ]
    # tf15m and tf5m break above high (120) with volume
    tf15m = [{"time": today_utc_midnight_ms + 4500000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 10}]

    # We want average volume of tf5m to be low, and the last candle's volume to be high
    # So 20 candles of volume 10, then the last candle (21st) of volume 100
    tf5m = [
        {"time": today_utc_midnight_ms + 4500000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 10}
        for _ in range(20)
    ]
    tf5m.append({"time": today_utc_midnight_ms + 4505000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 100})

    res_modular = modular_orb_engine(tf1h, tf15m, tf5m)
    assert res_modular["signal"] == "Buy"
    assert "1H opening range high broken" in res_modular["reason"]

    res_legacy = legacy_orb_engine(tf1h, tf15m, tf5m)
    assert res_legacy["signal"] == "Buy"
    assert "1H opening range high broken" in res_legacy["reason"]


def _sr_1h():
    rows = [
        {"time": index, "open": 100, "high": 110, "low": 90, "close": 100, "volume": 1000}
        for index in range(31)
    ]
    rows[-1] = {"time": 31, "open": 100, "high": 108, "low": 94, "close": 104, "volume": 1000}
    return rows


def _sr_15m(close=112.5):
    rows = [
        {"time": index, "open": 100, "high": 105, "low": 95, "close": 101, "volume": 1000}
        for index in range(29)
    ]
    rows.append({"time": 30, "open": 110, "high": 113, "low": 109, "close": close, "volume": 1300})
    return rows


def _sr_5m(last):
    rows = [
        {"time": index, "open": 100, "high": 104, "low": 96, "close": 101, "volume": 100}
        for index in range(19)
    ]
    rows.append({"time": 20, "open": 109.5, "high": 111, "low": 109, "close": 110.5, "volume": 100})
    rows.append(last)
    return rows


def test_sr_breakout_requires_body_volume_and_closed_confirmation():
    last = {"time": 21, "open": 110, "high": 113, "low": 109, "close": 112.5, "volume": 140}

    result = sr_breakout_engine(_sr_1h(), _sr_15m(), _sr_5m(last))

    assert result["signal"] == "Buy"
    assert result["strength"] >= 3
    assert "Confirmed resistance breakout" in result["reason"]


def test_sr_breakout_rejects_wick_only_false_breakout():
    last = {"time": 21, "open": 109, "high": 112, "low": 108, "close": 109.5, "volume": 160}

    result = sr_breakout_engine(_sr_1h(), _sr_15m(close=109.5), _sr_5m(last))

    assert result["signal"] == "WAIT"
    assert "False breakout" in result["reason"]


def _trend_1h():
    rows = []
    price = 100.0
    for index in range(60):
        close = price + 0.22
        rows.append({
            "time": index,
            "open": price,
            "high": close + 0.35,
            "low": price - 0.25,
            "close": close,
            "volume": 1000,
        })
        price = close
    return rows


def _trend_15m(overextended=False):
    rows = []
    price = 108.0
    for index in range(24):
        close = price + 0.03
        rows.append({
            "time": index,
            "open": price,
            "high": close + 0.18,
            "low": price - 0.12,
            "close": close,
            "volume": 1000,
        })
        price = close
    close = price + (1.8 if overextended else 0.04)
    rows.append({
        "time": 24,
        "open": price - 0.05,
        "high": close + 0.15,
        "low": price - 0.2,
        "close": close,
        "volume": 1100,
    })
    return rows


def _trend_5m():
    rows = [
        {"time": index, "open": 108.1, "high": 108.4, "low": 108.0, "close": 108.25, "volume": 100}
        for index in range(19)
    ]
    rows.append({"time": 19, "open": 108.35, "high": 108.6, "low": 108.2, "close": 108.5, "volume": 100})
    rows.append({"time": 20, "open": 108.45, "high": 109.0, "low": 108.4, "close": 108.9, "volume": 120})
    return rows


def test_trend_following_requires_pullback_reclaim_and_follow_through():
    result = trend_following_engine(_trend_1h(), _trend_15m(), _trend_5m())

    assert result["signal"] == "Buy"
    assert result["strength"] >= 3
    assert "EMA reclaim" in result["reason"]


def test_trend_following_rejects_overextended_chase_entry():
    result = trend_following_engine(_trend_1h(), _trend_15m(overextended=True), _trend_5m())

    assert result["signal"] == "WAIT"
    assert "overextended" in result["reason"]


def _vwap_15m(far=False):
    rows = []
    price = 108.0
    for index in range(39):
        close = price + 0.01
        rows.append({
            "time": index,
            "open": price,
            "high": close + 0.12,
            "low": price - 0.12,
            "close": close,
            "volume": 1000,
        })
        price = close
    close = price + (1.3 if far else 0.03)
    rows.append({
        "time": 39,
        "open": price - 0.04,
        "high": close + 0.12,
        "low": price - 0.35,
        "close": close,
        "volume": 1200,
    })
    return rows


def test_vwap_bounce_requires_reclaim_and_follow_through():
    result = vwap_bounce_engine(_trend_1h(), _vwap_15m(), _trend_5m())

    assert result["signal"] == "Buy"
    assert result["strength"] >= 3
    assert "VWAP reclaim" in result["reason"]


def test_vwap_bounce_rejects_far_from_vwap_chase_entry():
    result = vwap_bounce_engine(_trend_1h(), _vwap_15m(far=True), _trend_5m())

    assert result["signal"] == "WAIT"
    assert "too far from VWAP" in result["reason"] or "overextended" in result["reason"]


def _rsi_bullish_divergence_15m(confirm=True):
    closes = []
    price = 110.0
    for _ in range(10):
        price -= 0.2
        closes.append(price)
    for _ in range(5):
        price -= 1.0
        closes.append(price)
    for _ in range(8):
        price += 0.6
        closes.append(price)
    for _ in range(6):
        price -= 0.45
        closes.append(price)
    for _ in range(6):
        price += 0.25
        closes.append(price)

    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "time": index,
            "open": close + 0.1,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 1000,
        })
    rows[14]["low"] = 101.0
    rows[14]["close"] = 101.3
    rows[28]["low"] = 100.5 if confirm else 101.5
    rows[28]["close"] = 100.9 if confirm else 101.9
    return rows


def _rsi_reversal_5m():
    rows = [
        {"time": index, "open": 101, "high": 101.3, "low": 100.8, "close": 101.1, "volume": 100}
        for index in range(19)
    ]
    rows.append({"time": 19, "open": 101, "high": 101.2, "low": 100.7, "close": 100.9, "volume": 100})
    rows.append({"time": 20, "open": 100.85, "high": 101.6, "low": 100.8, "close": 101.45, "volume": 130})
    return rows


def test_rsi_divergence_uses_pivots_and_reversal_confirmation():
    result = rsi_divergence_engine(_trend_1h(), _rsi_bullish_divergence_15m(), _rsi_reversal_5m())

    assert result["signal"] == "Buy"
    assert result["strength"] >= 3
    assert "bullish pivot divergence" in result["reason"]


def test_rsi_divergence_rejects_missing_lower_low_pivot():
    result = rsi_divergence_engine(_trend_1h(), _rsi_bullish_divergence_15m(confirm=False), _rsi_reversal_5m())

    assert result["signal"] == "WAIT"
    assert "No confirmed pivot divergence" in result["reason"]


def _sweep_15m(confirm=True):
    rows = [
        {"time": index, "open": 102, "high": 104, "low": 100, "close": 102, "volume": 1000}
        for index in range(24)
    ]
    rows.append({
        "time": 24,
        "open": 101.8,
        "high": 102.5,
        "low": 99.4,
        "close": 101.4 if confirm else 99.8,
        "volume": 1600,
    })
    return rows


def _sweep_5m(confirm=True):
    rows = [
        {"time": index, "open": 101, "high": 101.3, "low": 100.8, "close": 101.05, "volume": 100}
        for index in range(19)
    ]
    rows.append({"time": 19, "open": 100.9, "high": 101.0, "low": 100.5, "close": 100.7, "volume": 100})
    if confirm:
        rows.append({"time": 20, "open": 100.75, "high": 101.7, "low": 100.7, "close": 101.55, "volume": 135})
    else:
        rows.append({"time": 20, "open": 100.75, "high": 101.0, "low": 100.4, "close": 100.55, "volume": 135})
    return rows


def test_liquidity_sweep_detects_bullish_stop_hunt_reclaim():
    result = liquidity_sweep_engine(_trend_1h(), _sweep_15m(), _sweep_5m())

    assert result["signal"] == "Buy"
    assert result["strength"] >= 3
    assert "Bullish stop-hunt sweep" in result["reason"]


def test_liquidity_sweep_rejects_without_reclaim_or_follow_through():
    result = liquidity_sweep_engine(_trend_1h(), _sweep_15m(confirm=False), _sweep_5m(confirm=False))

    assert result["signal"] == "WAIT"
    assert "No confirmed" in result["reason"] or "needs wick rejection" in result["reason"]


def test_balanced_router_blocks_low_strength_single_vote():
    result = route_votes(
        [{"engine": "Trend Follow", "signal": "Buy", "reason": "weak", "strength": 2}],
        "balanced",
    )

    assert result["decision"] == "WAIT"
    assert "single Buy vote strength" in result["reason"]


def test_aggressive_router_still_blocks_very_low_strength_single_vote():
    result = route_votes(
        [{"engine": "Trend Follow", "signal": "Buy", "reason": "weak", "strength": 1.5}],
        "aggressive",
    )

    assert result["decision"] == "WAIT"
    assert "single Buy vote strength" in result["reason"]


def test_balanced_router_accepts_confirmed_single_vote():
    result = route_votes(
        [{"engine": "S/R Breakout", "signal": "Buy", "reason": "confirmed", "strength": 3.5}],
        "balanced",
    )

    assert result["decision"] == "Buy"


def test_journal_engine_concurrency(tmp_path):
    import threading
    from engines.journal import JournalEngine

    # Use a temporary file for the journal path
    journal_file = tmp_path / "trade_journal.json"
    engine = JournalEngine(limit=10, path=journal_file)

    num_threads = 10
    entries_per_thread = 5

    def worker(worker_id):
        for i in range(entries_per_thread):
            engine.add("TEST_EVENT", {"worker_id": worker_id, "index": i})

    threads = []
    for t_id in range(num_threads):
        t = threading.Thread(target=worker, args=(t_id,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # The entries should be capped at limit=10
    assert len(engine.entries) <= 10

    # Let's verify that the entries loaded from file are also correct and match the in-memory state
    loaded_engine = JournalEngine(limit=10, path=journal_file)
    assert len(loaded_engine.entries) == len(engine.entries)
    for entry in loaded_engine.entries:
        assert entry["event"] == "TEST_EVENT"
        assert "worker_id" in entry["payload"]
        assert "index" in entry["payload"]
