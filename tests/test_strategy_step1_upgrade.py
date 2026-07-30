from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend import strategy_step1_upgrade


def _candle(dt, open_, high, low, close, volume=100):
    return {
        "time": int(dt.timestamp() * 1000),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


def _trend_candles(start, count, minutes, base=100.0, step=0.2):
    rows = []
    price = base
    for index in range(count):
        current = price + (index * step)
        rows.append(_candle(start + timedelta(minutes=index * minutes), current - 0.1, current + 0.3, current - 0.2, current + 0.2, 100 + index))
    return rows


def test_london_orb_uses_configured_session_range(monkeypatch):
    monkeypatch.setenv("ORB_TIMEZONE", "Asia/Dhaka")
    monkeypatch.setenv("ORB_LONDON_START", "14:00")
    monkeypatch.setenv("ORB_LONDON_END", "17:00")
    monkeypatch.setenv("ORB_RANGE_MINUTES", "60")

    tz = ZoneInfo("Asia/Dhaka")
    day = datetime(2026, 7, 29, 0, 0, tzinfo=tz)
    tf1h = _trend_candles(day - timedelta(hours=60), 60, 60, 90, 0.3)
    tf15 = _trend_candles(day + timedelta(hours=8), 24, 15, 98, 0.02)

    opening_indexes = [24, 25, 26, 27]
    for idx, minute in zip(opening_indexes, [0, 15, 30, 45]):
        tf15.append(_candle(day.replace(hour=14, minute=minute), 100, 101, 99, 100.5, 120))
    tf15.append(_candle(day.replace(hour=15, minute=0), 101, 102.5, 100.8, 102.0, 180))

    tf5 = _trend_candles(day.replace(hour=13, minute=15), 21, 5, 99, 0.03)
    tf5.append(_candle(day.replace(hour=15, minute=0), 101.0, 102.4, 100.9, 102.1, 260))

    result = strategy_step1_upgrade.session_orb_engine(tf1h, tf15, tf5)

    assert result["signal"] == "Buy"
    assert result["session"] == "LONDON"
    assert result["rangeHigh"] == 101.0
    assert "session range high broken" in result["reason"]


def test_orb_waits_outside_london_and_new_york_sessions(monkeypatch):
    monkeypatch.setenv("ORB_TIMEZONE", "Asia/Dhaka")
    tz = ZoneInfo("Asia/Dhaka")
    day = datetime(2026, 7, 29, 9, 0, tzinfo=tz)
    tf1h = _trend_candles(day - timedelta(hours=60), 60, 60)
    tf15 = _trend_candles(day - timedelta(hours=10), 60, 15)
    tf5 = _trend_candles(day - timedelta(hours=2), 30, 5)

    result = strategy_step1_upgrade.session_orb_engine(tf1h, tf15, tf5)

    assert result["signal"] == "WAIT"
    assert "Outside configured" in result["reason"]


def test_mtf_confluence_requires_full_direction_alignment():
    tz = ZoneInfo("Asia/Dhaka")
    day = datetime(2026, 7, 29, 10, 0, tzinfo=tz)
    tf1h = _trend_candles(day - timedelta(hours=60), 60, 60, 80, 0.5)
    tf15 = _trend_candles(day - timedelta(minutes=55 * 15), 55, 15, 90, 0.2)
    tf5 = _trend_candles(day - timedelta(minutes=4 * 5), 4, 5, 100, 0.3)

    result = strategy_step1_upgrade.confluence(tf1h, tf15, tf5)

    assert result["ready"] is True
    assert result["direction"] == "Buy"
    assert result["direction1H"] == "Buy"
    assert result["direction15M"] == "Buy"
    assert result["direction5M"] == "Buy"


def test_conflicting_actionable_vote_is_changed_to_wait():
    mtf = {
        "ready": True,
        "direction": "Buy",
        "direction1H": "Buy",
        "direction15M": "Buy",
        "direction5M": "Buy",
        "reason": "1H, 15M, and 5M aligned",
    }
    votes = [
        {"engine": "ORB", "signal": "Sell", "reason": "breakdown", "strength": 4.2},
        {"engine": "Trend Follow", "signal": "Buy", "reason": "aligned", "strength": 4.0},
    ]

    filtered = strategy_step1_upgrade._filter_votes(votes, mtf)

    assert filtered[0]["signal"] == "WAIT"
    assert filtered[0]["strength"] == 0
    assert filtered[1]["signal"] == "Buy"
