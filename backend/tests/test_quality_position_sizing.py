from decimal import Decimal
from types import SimpleNamespace

from backend import cost_policy_fix


def test_quality_grades_require_two_aligned_votes():
    assert cost_policy_fix.classify_quality(
        {"side": "Buy", "strategyVotes": [{"signal": "Buy"}]}
    ) == {"grade": "B+", "riskPct": 0.0, "alignedVotes": 1, "eligible": False}

    assert cost_policy_fix.classify_quality(
        {
            "side": "Buy",
            "strategyVotes": [{"signal": "Buy"}, {"signal": "Buy"}, {"signal": "WAIT"}],
        }
    ) == {"grade": "A", "riskPct": 0.75, "alignedVotes": 2, "eligible": True}

    assert cost_policy_fix.classify_quality(
        {
            "side": "Sell",
            "strategyVotes": [
                {"signal": "Sell"},
                {"signal": "Sell"},
                {"signal": "Sell"},
            ],
        }
    ) == {"grade": "A+", "riskPct": 1.0, "alignedVotes": 3, "eligible": True}


def fake_core():
    return SimpleNamespace(
        get_mark_price=lambda symbol: 100.0,
        get_wallet_equity=lambda: (958.0, "OK"),
        get_instrument_rules=lambda symbol: {
            "ok": True,
            "qtyStep": Decimal("0.001"),
            "minOrderQty": Decimal("0.001"),
            "maxOrderQty": Decimal("1000"),
            "minNotionalValue": Decimal("5"),
        },
        floor_to_step=lambda value, step: (Decimal(str(value)) // step) * step,
        format_qty=lambda value: format(Decimal(str(value)).normalize(), "f"),
    )


def test_a_plus_risk_is_capped_at_one_percent_without_allocation_cap():
    cost_policy_fix._CONTEXT.candidate = {
        "qualityGrade": "A+",
        "qualityRiskPct": 1.0,
        "alignedVotes": 3,
    }
    result = cost_policy_fix._quality_sizing(
        fake_core(),
        "BTCUSDT",
        {"stopLossPct": 0.8},
    )
    assert result["ok"] is True
    assert result["qualityGrade"] == "A+"
    assert result["riskPerTradePct"] == 1.0
    assert result["allocationCapApplied"] is False
    assert result["actualStopRiskUsdt"] <= 9.58
    assert result["actualRiskPct"] <= 1.0
    assert float(result["estimatedNotional"]) > 250.0


def test_a_grade_uses_three_quarter_percent_risk():
    cost_policy_fix._CONTEXT.candidate = {
        "qualityGrade": "A",
        "qualityRiskPct": 0.75,
        "alignedVotes": 2,
    }
    result = cost_policy_fix._quality_sizing(
        fake_core(),
        "ETHUSDT",
        {"stopLossPct": 1.0},
    )
    assert result["ok"] is True
    assert result["riskPerTradePct"] == 0.75
    assert result["actualStopRiskUsdt"] <= 7.185
    assert result["actualRiskPct"] <= 0.75


def test_missing_or_b_plus_quality_fails_closed():
    cost_policy_fix._CONTEXT.candidate = {
        "qualityGrade": "B+",
        "qualityRiskPct": 0.0,
        "alignedVotes": 1,
    }
    result = cost_policy_fix._quality_sizing(
        fake_core(),
        "XRPUSDT",
        {"stopLossPct": 0.8},
    )
    assert result["ok"] is False
    assert result["code"] == "QUALITY_GRADE_BLOCKED"
    assert result["qty"] == "0"
