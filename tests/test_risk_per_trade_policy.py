from __future__ import annotations

import pytest

from backend.batch1_safety import RISK_PER_TRADE_PCT, validate_start_payload


def _defaults():
    return {
        "symbol": "BTCUSDT",
        "interval": "5",
        "qty": "0.001",
        "maxAllocationUsdt": 250,
        "riskPerTradePct": 0.25,
        "maxOpenPositions": 3,
        "dailyLossCapUsdt": 25,
        "maxTradesPerDay": 6,
        "stopLossPct": 0.8,
        "takeProfitPct": 1.6,
        "breakevenTriggerPct": 0.6,
        "partialTpTriggerPct": 1.4,
        "partialTpClosePct": 40,
        "trailingStopTriggerPct": 1.8,
        "trailingStopDistancePct": 0.45,
        "cooldownSeconds": 180,
        "mode": "conservative",
    }


def test_start_defaults_to_two_percent_even_with_legacy_state():
    config = validate_start_payload({}, _defaults())
    assert RISK_PER_TRADE_PCT == 2.0
    assert config["riskPerTradePct"] == 2.0


def test_explicit_two_percent_is_accepted():
    config = validate_start_payload({"riskPerTradePct": 2}, _defaults())
    assert config["riskPerTradePct"] == 2.0


@pytest.mark.parametrize("value", [0.25, 0.5, 1, 1.99, 2.01, 3])
def test_any_other_risk_percentage_is_rejected(value):
    with pytest.raises(ValueError, match="locked at 2%"):
        validate_start_payload({"riskPerTradePct": value}, _defaults())
