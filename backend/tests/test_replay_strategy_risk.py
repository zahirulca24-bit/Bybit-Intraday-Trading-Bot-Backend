from __future__ import annotations

from decimal import Decimal

from backend import replay_strategy_risk as strategy


def _candles(count=60, slope=1):
    rows = []
    price = Decimal("100")
    for index in range(count):
        price += Decimal(str(slope))
        rows.append({
            "openTime": 1_800_000_000_000 + index * 300_000,
            "open": str(price - Decimal("0.5")),
            "high": str(price + Decimal("1")),
            "low": str(price - Decimal("1")),
            "close": str(price),
            "volume": str(100 + index),
        })
    return rows


def test_insufficient_history_fails_closed_without_risk_candidate():
    result = strategy.evaluate(_candles(20), {"equity": "1000"})
    assert result["evaluated"] is False
    assert result["signal"] == "WAIT"
    assert result["eligible"] is False
    assert result["risk"] is None
    assert result["externalExecutionAllowed"] is False


def test_bullish_history_produces_candidate_only_risk_plan():
    result = strategy.evaluate(_candles(60, 1), {"equity": "1000"})
    assert result["evaluated"] is True
    assert result["signal"] in {"Buy", "WAIT"}
    if result["eligible"]:
        assert result["grade"] in {"A", "A+"}
        assert result["risk"]["riskPct"] in {"0.75", "1.00"}
        assert Decimal(result["risk"]["riskAmount"]) <= Decimal("10")
        assert result["risk"]["rewardRisk"] == "2"
        assert result["risk"]["sizingStatus"] == "candidate_only"
    assert result["executionSimulated"] is False
    assert result["externalExecutionAllowed"] is False


def test_bearish_history_never_creates_external_execution_permission():
    result = strategy.evaluate(_candles(60, -1), {"equity": "2500"})
    assert result["signal"] in {"Sell", "WAIT"}
    assert result["executionSimulated"] is False
    assert result["externalExecutionAllowed"] is False


def test_indicator_output_is_deterministic_for_same_history():
    candles = _candles(70, 0.25)
    session = {"equity": "1000", "strategyMode": "balanced"}
    assert strategy.evaluate(candles, session) == strategy.evaluate(candles, session)
