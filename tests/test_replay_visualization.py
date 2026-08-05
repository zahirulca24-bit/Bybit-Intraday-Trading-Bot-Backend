from decimal import Decimal
from pathlib import Path

import pytest

from backend import replay_visualization as visualization


def test_query_defaults_block_future_and_bound_candles():
    options = visualization.normalize_query({})
    assert options == {"limit": 500, "includeFuture": False}
    assert visualization.normalize_query({"limit": "1000", "includeFuture": "true"}) == {
        "limit": 1000,
        "includeFuture": True,
    }
    with pytest.raises(visualization.ReplayVisualizationValidationError):
        visualization.normalize_query({"limit": "1001"})
    with pytest.raises(visualization.ReplayVisualizationValidationError):
        visualization.normalize_query({"includeFuture": "maybe"})


def test_trade_visualization_exposes_truthful_markers_and_r_multiple():
    trade = {
        "tradeId": "sim_trade_001",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "status": "CLOSED",
        "entryTime": 1_000,
        "exitTime": 2_000,
        "entryPrice": "100",
        "exitPrice": "104",
        "quantity": "2",
        "realizedPnl": "7.4",
        "fees": "0.6",
        "payload": {
            "stopLoss": "98",
            "takeProfit": "104",
            "riskAmount": "4",
            "grossPnl": "8",
            "netPnl": "7.4",
            "exitReason": "take_profit",
            "sameCandleConflict": False,
            "sameCandlePolicy": "stop_first",
        },
    }

    result = visualization.trade_visualization(trade)

    assert result["rMultiple"] == "1.8500"
    assert result["holdingDurationMs"] == 1_000
    assert result["grossPnl"] == "8.00000000"
    assert result["fees"] == "0.60000000"
    assert result["netPnl"] == "7.40000000"
    assert result["exitReason"] == "take_profit"
    assert [marker["type"] for marker in result["markers"]] == [
        "entry",
        "stop_loss",
        "take_profit",
        "exit",
    ]


def test_trade_visualization_does_not_fabricate_r_without_risk_amount():
    result = visualization.trade_visualization(
        {
            "tradeId": "sim_trade_002",
            "symbol": "ETHUSDT",
            "side": "Sell",
            "status": "OPEN",
            "entryTime": 1_000,
            "exitTime": None,
            "entryPrice": "200",
            "exitPrice": None,
            "quantity": "1",
            "realizedPnl": "0",
            "fees": "0.1",
            "payload": {"stopLoss": "204", "takeProfit": "192"},
        }
    )
    assert result["rMultiple"] is None
    assert result["exitReason"] is None
    assert result["holdingDurationMs"] is None
    assert all(marker["type"] != "exit" for marker in result["markers"])


def test_endpoint_is_read_only_and_wired_before_session_fallback():
    server = Path("backend/secure_server.py").read_text(encoding="utf-8")
    module = Path("backend/replay_visualization.py").read_text(encoding="utf-8")

    assert "replay_visualization.handle_get(self, core, path)" in server
    assert server.index("replay_visualization.handle_get(self, core, path)") < server.index(
        "replay_sessions.handle_get(self, core, path)"
    )
    assert "replay_visualization.install(core)" in server
    assert "def handle_get(" in module
    assert "def handle_post(" not in module
    assert "externalExecutionAllowed\": False" in module
    assert "exchangeCredentialsUsed\": False" in module
    assert "activeSessionLookaheadBlocked\": True" in module
    assert "stop_first" in module


def test_money_and_r_quantization_are_deterministic():
    assert visualization._money(Decimal("1.234567899")) == "1.23456789"
    assert visualization._r(Decimal("1.23459")) == "1.2345"
