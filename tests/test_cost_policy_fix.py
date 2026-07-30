from backend import cost_policy_fix


class FakeScanner:
    @staticmethod
    def settings():
        return {
            "minimumGrossRr": 2.0,
            "minimumNetRr": 1.7,
            "preferredNetRr": 2.0,
            "maximumCostRiskPct": 35.0,
            "normalCostRiskPct": 15.0,
        }


NORMAL_MARKET = {
    "ok": True,
    "spreadPct": 0.06,
    "spreadTier": "normal",
    "slippagePct": 0.03,
    "estimatedRoundTripFeePct": 0.11,
    "estimatedTotalCostPct": 0.20,
}


def test_extends_two_r_target_when_costs_reduce_net_rr():
    result = cost_policy_fix.evaluate_cost_policy(
        stop_pct=1.0,
        take_pct=2.0,
        market_cost=NORMAL_MARKET,
        scanner_module=FakeScanner,
    )

    assert result["ok"] is True
    assert result["targetAdjusted"] is True
    assert result["adjustedTakeProfitPct"] == 2.24
    assert result["grossRr"] == 2.24
    assert result["netRr"] >= 1.7


def test_keeps_better_existing_target():
    result = cost_policy_fix.evaluate_cost_policy(
        stop_pct=1.0,
        take_pct=3.0,
        market_cost=NORMAL_MARKET,
        scanner_module=FakeScanner,
    )

    assert result["ok"] is True
    assert result["targetAdjusted"] is False
    assert result["adjustedTakeProfitPct"] == 3.0


def test_returns_exact_wide_spread_reason():
    result = cost_policy_fix.evaluate_cost_policy(
        stop_pct=1.0,
        take_pct=2.0,
        market_cost={
            **NORMAL_MARKET,
            "spreadPct": 0.25,
            "spreadTier": "blocked",
        },
        scanner_module=FakeScanner,
    )

    assert result["ok"] is False
    assert result["blockCode"] == "BLOCKED_WIDE_SPREAD"
    assert "maximum spread" in result["reason"]


def test_returns_exact_cost_to_risk_reason():
    result = cost_policy_fix.evaluate_cost_policy(
        stop_pct=0.4,
        take_pct=2.0,
        market_cost=NORMAL_MARKET,
        scanner_module=FakeScanner,
    )

    assert result["ok"] is False
    assert result["blockCode"] == "BLOCKED_COST_TO_RISK"
    assert result["costRiskPct"] == 50.0


def test_invalid_stop_fails_closed_with_specific_code():
    result = cost_policy_fix.evaluate_cost_policy(
        stop_pct=0.0,
        take_pct=2.0,
        market_cost=NORMAL_MARKET,
        scanner_module=FakeScanner,
    )

    assert result["ok"] is False
    assert result["blockCode"] == "BLOCKED_INVALID_STOP"
