import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.hardened_strategies import (
    _confirmed_retest,
    _five_minute_confirmation_after_15m,
    _pivot_is_fresh,
    install as install_hardened,
    liquidity_sweep_engine,
)
from engines.router import route_votes
from replay_accuracy import replay

BASE = 1_700_000_000_000


def candle(time, open_=100, high=101, low=99, close=100, volume=100):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close, "volume": volume}


def flat_1h():
    return [candle(BASE - (60 - index) * 3_600_000, volume=1000) for index in range(60)]


def sweep_15m():
    rows = [candle(BASE + index * 900_000, 102, 104, 100, 102, 1000) for index in range(24)]
    rows.append(candle(BASE + 24 * 900_000, 101.8, 102.5, 99.4, 101.4, 1600))
    return rows


def sweep_5m(aligned=True):
    sweep_open = BASE + 24 * 900_000
    confirm_open = sweep_open + (900_000 if aligned else 600_000)
    rows = [
        candle(confirm_open - (20 - index) * 300_000, 101, 101.3, 100.8, 101.05, 100)
        for index in range(20)
    ]
    rows[-1] = candle(confirm_open - 300_000, 100.9, 101, 100.5, 100.7, 100)
    rows.append(candle(confirm_open, 100.75, 101.7, 100.7, 101.55, 135))
    return rows


def test_breakout_retest_requires_prior_breakout_close():
    initial_break = _confirmed_retest(
        {"close": 99, "low": 98, "high": 100},
        {"close": 101, "low": 99, "high": 102},
        100,
        "Buy",
    )
    real_retest = _confirmed_retest(
        {"close": 101, "low": 100.5, "high": 102},
        {"close": 101.2, "low": 99.9, "high": 101.5},
        100,
        "Buy",
    )
    assert initial_break is False
    assert real_retest is True


def test_liquidity_sweep_requires_chronological_5m_confirmation():
    aligned = liquidity_sweep_engine(flat_1h(), sweep_15m(), sweep_5m(True))
    stale = liquidity_sweep_engine(flat_1h(), sweep_15m(), sweep_5m(False))
    assert aligned["signal"] == "Buy"
    assert aligned["setupKey"].startswith("liquidity-sweep:Buy:")
    assert stale["signal"] == "WAIT"
    assert "chronologically aligned" in stale["reason"]


def test_confirmation_window_uses_15m_close_time():
    sweep = {"time": BASE}
    assert _five_minute_confirmation_after_15m(sweep, {"time": BASE + 900_000}) is True
    assert _five_minute_confirmation_after_15m(sweep, {"time": BASE + 600_000}) is False
    assert _five_minute_confirmation_after_15m(sweep, {"time": BASE + 900_000 + 21 * 60_000}) is False


def test_divergence_pivot_expiry_gate():
    assert _pivot_is_fresh(28, 35) is True
    assert _pivot_is_fresh(20, 35) is False


def test_router_blocks_weak_single_vote_but_accepts_strong_vote():
    weak = route_votes([{"engine": "Trend Follow", "signal": "Buy", "strength": 2}], "balanced")
    strong = route_votes([{"engine": "S/R Breakout", "signal": "Buy", "strength": 3.5}], "balanced")
    assert weak["decision"] == "WAIT"
    assert strong["decision"] == "Buy"


def test_install_replaces_canonical_strategy_and_router_sources():
    core = SimpleNamespace()
    install_hardened(core)
    assert core._hardened_strategies_installed is True
    assert core.liquidity_sweep_engine is liquidity_sweep_engine
    assert core.route_votes is route_votes


def test_replay_evaluates_liquidity_sweep_vote():
    entry = [candle(BASE + index * 300_000, 100, 101, 99, 100, 100) for index in range(130)]
    tf15 = [candle(BASE - 100 * 900_000 + index * 900_000, 100, 101, 99, 100, 100) for index in range(180)]
    tf1h = [candle(BASE - 100 * 3_600_000 + index * 3_600_000, 100, 101, 99, 100, 100) for index in range(180)]
    calls = {"liquidity": 0}

    def fetch(symbol, interval, limit=120):
        return ({"5": entry, "15": tf15, "60": tf1h}[interval], "OK")

    def wait_vote(name):
        return {"engine": name, "signal": "WAIT", "reason": "wait", "strength": 0}

    def liquidity(*_):
        calls["liquidity"] += 1
        return {"engine": "Liquidity Sweep", "signal": "WAIT", "reason": "checked", "strength": 0}

    core = SimpleNamespace(
        fetch_candles=fetch,
        trend_following_engine=lambda *_: wait_vote("Trend Follow"),
        sr_breakout_engine=lambda *_: wait_vote("S/R Breakout"),
        rsi_divergence_engine=lambda *_: wait_vote("RSI Divergence"),
        vwap_bounce_engine=lambda *_: wait_vote("VWAP Bounce"),
        liquidity_sweep_engine=liquidity,
        route_votes=lambda votes, mode: {"decision": "WAIT", "reason": "wait"},
        normalize_mode=lambda mode: mode,
        estimate_trade_outcome=lambda *args: ("flat", 1, args[1]),
        vote=lambda engine, signal, reason, strength=0: {
            "engine": engine,
            "signal": signal,
            "reason": reason,
            "strength": strength,
        },
        avg_volume=lambda rows, period=20: 100,
    )
    result = replay(core, "BTCUSDT")
    assert result["ok"] is True
    assert calls["liquidity"] > 0
    assert "Liquidity Sweep" in result["votesByEngine"]
    assert result["methodology"]["strategyParity"] is True
