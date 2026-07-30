import os
import sys
from decimal import Decimal


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.risk import RiskEngine, signal_risk_policy
import server


def test_signal_risk_blocks_very_weak_single_vote():
    state = {
        "engineVotes": [
            {"engine": "Trend Follow", "signal": "Buy", "strength": 1.5},
        ],
        "router": {"confidence": 1},
    }

    policy = signal_risk_policy(state, "Buy")

    assert policy["ok"] is False
    assert "weak_single_vote" in policy["riskFlags"]


def test_signal_risk_reduces_moderate_single_vote_size():
    state = {
        "engineVotes": [
            {"engine": "VWAP Bounce", "signal": "Buy", "strength": 2.5},
        ],
        "router": {"confidence": 1},
    }

    policy = signal_risk_policy(state, "Buy")

    assert policy["ok"] is True
    assert policy["sizeFactor"] == 0.5
    assert "single_vote_reduced_size" in policy["riskFlags"]


def test_signal_risk_blocks_four_loss_streak():
    state = {
        "engineVotes": [
            {"engine": "S/R Breakout", "signal": "Sell", "strength": 4.5},
        ],
        "router": {"confidence": 1},
        "consecutiveLosses": 4,
    }

    policy = signal_risk_policy(state, "Sell")

    assert policy["ok"] is False
    assert "losing_streak_block" in policy["riskFlags"]


def test_risk_engine_stores_structured_decision():
    engine = RiskEngine(
        position_size_fn=lambda _symbol: (0.0, "OK"),
        open_positions_count_fn=lambda: (0, "OK"),
    )
    state = {
        "symbol": "BTCUSDT",
        "cooldownSeconds": 0,
        "maxOpenPositions": 3,
        "engineVotes": [
            {"engine": "VWAP Bounce", "signal": "Buy", "strength": 2.5},
        ],
        "router": {"confidence": 1},
    }

    approved, reason = engine.check(state, "Buy")

    assert approved is True
    assert "0.50x size factor" in reason
    assert state["riskDecision"]["sizeFactor"] == 0.5
    assert state["riskSizeFactor"] == 0.5


def test_position_sizing_applies_signal_risk_factor():
    original_mark = server.get_mark_price
    original_equity = server.get_wallet_equity
    original_rules = server.get_instrument_rules
    try:
        server.get_mark_price = lambda _symbol: 100.0
        server.get_wallet_equity = lambda: (1000.0, "OK")
        server.get_instrument_rules = lambda _symbol: {
            "ok": True,
            "qtyStep": Decimal("0.001"),
            "minOrderQty": Decimal("0.001"),
            "maxOrderQty": Decimal("0"),
            "minNotionalValue": Decimal("5"),
        }
        base = {
            "riskPerTradePct": 1.0,
            "stopLossPct": 1.0,
            "maxAllocationUsdt": 1000.0,
            "signal": "Buy",
            "engineVotes": [{"engine": "VWAP Bounce", "signal": "Buy", "strength": 2.5}],
            "router": {"confidence": 1},
        }

        sizing = server.calculate_position_sizing("BTCUSDT", base)

        assert sizing["ok"] is True
        assert sizing["riskSizeFactor"] == 0.5
        assert sizing["effectiveRiskPerTradePct"] == 0.5
        assert sizing["qty"] == "5"
    finally:
        server.get_mark_price = original_mark
        server.get_wallet_equity = original_equity
        server.get_instrument_rules = original_rules
