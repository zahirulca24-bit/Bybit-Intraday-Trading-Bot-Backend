from types import SimpleNamespace

import pytest

from backend import intraday_scanner
from backend import scanner_execution_gate


def test_locked_defaults(monkeypatch):
    for key in list(intraday_scanner.os.environ):
        if key in {
            "UNIVERSE_SHORTLIST_SIZE", "MAX_SCAN_SYMBOLS", "NORMAL_SPREAD_PCT",
            "REDUCED_SIZE_SPREAD_PCT", "MAX_SPREAD_PCT", "MIN_GROSS_RR",
            "MIN_NET_RR", "PREFERRED_NET_RR", "NORMAL_ENTRY_COST_RISK_PCT",
            "MAX_ENTRY_COST_RISK_PCT", "UNIVERSE_REFRESH_SECONDS",
            "SCAN_DEADLINE_SECONDS",
        }:
            monkeypatch.delenv(key, raising=False)
    cfg = intraday_scanner.settings()
    assert cfg["shortlistSize"] == 20
    assert cfg["deepScanSize"] == 10
    assert cfg["normalSpreadPct"] == 0.08
    assert cfg["reducedSpreadPct"] == 0.15
    assert cfg["maxSpreadPct"] == 0.20
    assert cfg["minimumGrossRr"] == 2.0
    assert cfg["minimumNetRr"] == 1.70
    assert cfg["preferredNetRr"] == 2.0
    assert cfg["normalCostRiskPct"] == 15.0
    assert cfg["maximumCostRiskPct"] == 35.0
    assert cfg["refreshSeconds"] == 600
    assert cfg["deadlineSeconds"] == 20.0


def test_only_intraday_intervals_are_allowed():
    for value in ("5", "15", "30", "60"):
        assert intraday_scanner.normalize_scanner_interval(value) == value
    for value in ("1", "3", "120", "D", "garbage"):
        with pytest.raises(ValueError):
            intraday_scanner.normalize_scanner_interval(value)


def test_symbol_validation_caps_at_ten():
    requested = [f"COIN{i}USDT" for i in range(15)] + ["../BAD", "BTCUSD", ""]
    accepted, rejected = intraday_scanner.normalize_symbols(requested, 10)
    assert len(accepted) == 10
    assert all(symbol.endswith("USDT") for symbol in accepted)
    assert "../BAD" in rejected
    assert "BTCUSD" in rejected
    assert "COIN14USDT" in rejected


def test_spread_tiers_match_approved_policy():
    assert intraday_scanner.spread_tier(0.08) == "normal"
    assert intraday_scanner.spread_tier(0.081) == "reduced"
    assert intraday_scanner.spread_tier(0.15) == "reduced"
    assert intraday_scanner.spread_tier(0.151) == "strong_only"
    assert intraday_scanner.spread_tier(0.20) == "strong_only"
    assert intraday_scanner.spread_tier(0.201) == "blocked"


class CostCore:
    def __init__(self, spread_pct):
        self.spread_pct = spread_pct

    def public_bybit_get(self, path, params):
        last = 100.0
        half = self.spread_pct / 200 * last
        return {
            "retCode": 0,
            "result": {"list": [{"lastPrice": str(last), "bid1Price": str(last - half), "ask1Price": str(last + half)}]},
        }


def test_cost_gate_blocks_dayforge_style_large_friction(monkeypatch):
    monkeypatch.setenv("ESTIMATED_TAKER_FEE_PCT", "0")
    monkeypatch.setenv("SLIPPAGE_SPREAD_MULTIPLIER", "0")
    result = intraday_scanner.estimate_trade_cost(
        CostCore(0.20), "BTCUSDT", notional=7500, risk_amount=20, stop_pct=0.8, take_pct=1.6
    )
    assert result["estimatedCostUsdt"] == 15.0
    assert result["costRiskPct"] == 75.0
    assert result["ok"] is False


def test_cost_gate_reduces_size_in_middle_tier(monkeypatch):
    monkeypatch.setenv("ESTIMATED_TAKER_FEE_PCT", "0")
    monkeypatch.setenv("SLIPPAGE_SPREAD_MULTIPLIER", "0")
    result = intraday_scanner.estimate_trade_cost(
        CostCore(0.10), "BTCUSDT", notional=2000, risk_amount=20, stop_pct=1.0, take_pct=2.2
    )
    assert result["ok"] is True
    assert result["spreadTier"] == "reduced"
    assert result["sizeFactor"] == 0.5


def test_atr_volume_fetches_enough_closed_history():
    requested = []
    candles = [
        {"close": 100 + i * 0.01, "high": 100.3 + i * 0.01, "low": 99.7 + i * 0.01, "volume": 100 + i}
        for i in range(80)
    ]
    core = SimpleNamespace(
        fetch_candles=lambda symbol, interval, limit: (requested.append(limit) or candles, "OK"),
        simple_atr=lambda highs, lows, closes, period: 0.6,
    )
    atr, ratio = scanner_execution_gate._atr_volume_with_closed_history(core, "BTCUSDT")
    assert requested == [80]
    assert atr is not None
    assert ratio is not None
