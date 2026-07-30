from __future__ import annotations

import pytest

from backend.replay_safety import (
    ReplaySafetyViolation,
    assert_public_market_request,
    block_external_exchange_action,
    policy_status,
    unavailable_payload,
    validate_replay_request,
)


def test_replay_policy_is_permanently_simulation_only():
    status = policy_status()
    assert status["runtimeMode"] == "historical_replay"
    assert status["executionMode"] == "simulated_only"
    assert status["externalExecutionAllowed"] is False
    assert status["exchangeOrderRoutesAllowed"] is False
    assert status["privateExchangeApiAllowed"] is False
    assert status["publicMarketDataReadOnly"] is True
    assert status["stepEngineImplemented"] is True
    assert status["strategyReplayImplemented"] is True
    assert status["riskReplayImplemented"] is True
    assert status["simulatedExecutionImplemented"] is True
    assert "fees" in status["simulatedExecutionCapabilities"]
    assert "durable_idempotency" in status["simulatedExecutionCapabilities"]
    assert status["performanceSummaryImplemented"] is True
    assert status["replayJournalImplemented"] is True
    assert "max_drawdown" in status["performanceSummaryCapabilities"]
    assert "stable_sequence_pagination" in status["replayJournalCapabilities"]


def test_safe_replay_payload_is_normalized_without_execution_permission():
    payload = validate_replay_request(
        {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "strategyMode": "conservative",
            "executionMode": "simulated",
        }
    )
    assert payload["runtimeMode"] == "historical_replay"
    assert payload["executionMode"] == "simulated_only"
    assert payload["externalExecutionAllowed"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"confirmDemoOrder": True},
        {"execute": "yes"},
        {"settings": {"submitOrder": 1}},
        {"executionMode": "demo"},
        {"executionMode": "live"},
        {"apiKey": "secret"},
        {"exchangeEndpoint": "https://api-demo.bybit.com"},
    ],
)
def test_replay_rejects_exchange_execution_intent(payload):
    with pytest.raises(ReplaySafetyViolation, match="simulation-only"):
        validate_replay_request(payload)


def test_only_allowlisted_public_market_get_is_allowed():
    assert assert_public_market_request("GET", "/v5/market/kline?category=linear") == (
        "GET",
        "/v5/market/kline",
    )
    with pytest.raises(ReplaySafetyViolation, match="only GET"):
        assert_public_market_request("POST", "/v5/market/kline")
    with pytest.raises(ReplaySafetyViolation, match="not allowlisted"):
        assert_public_market_request("GET", "/v5/order/create")
    with pytest.raises(ReplaySafetyViolation, match="not allowlisted"):
        assert_public_market_request("GET", "/v5/position/list")


def test_external_exchange_action_boundary_always_blocks():
    with pytest.raises(ReplaySafetyViolation, match="cannot perform submit order"):
        block_external_exchange_action("submit order")


def test_future_frontend_capability_fails_closed_with_current_safety_contract():
    payload = unavailable_payload("frontend_integration")
    assert payload["ok"] is False
    assert payload["code"] == "REPLAY_NOT_IMPLEMENTED"
    assert payload["capability"] == "frontend_integration"
    assert payload["safety"]["externalExecutionAllowed"] is False
    assert payload["safety"]["stepEngineImplemented"] is True
    assert payload["safety"]["simulatedExecutionImplemented"] is True
    assert payload["safety"]["performanceSummaryImplemented"] is True
    assert payload["safety"]["replayJournalImplemented"] is True
