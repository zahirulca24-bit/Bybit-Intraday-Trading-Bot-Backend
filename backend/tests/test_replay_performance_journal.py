from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend import replay_performance_journal as analytics

SESSION = {
    "sessionId": "replay_test_0001",
    "symbol": "BTCUSDT",
    "timeframe": "5",
    "status": "COMPLETED",
    "startTime": 1_800_000_000_000,
    "endTime": 1_800_000_900_000,
    "cursorTime": 1_800_000_900_000,
    "initialBalance": "1000",
    "balance": "1045",
    "equity": "1045",
    "strategyMode": "balanced",
    "config": {},
    "summary": {},
    "createdAt": 1,
    "updatedAt": 2,
}


def trade(
    trade_id,
    pnl,
    *,
    side="Buy",
    status="CLOSED",
    entry=1_800_000_000_000,
    exit_time=1_800_000_300_000,
    fees="5",
    risk="50",
):
    return {
        "tradeId": trade_id,
        "symbol": "BTCUSDT",
        "side": side,
        "status": status,
        "entryTime": entry,
        "exitTime": exit_time if status == "CLOSED" else None,
        "entryPrice": "100",
        "exitPrice": "110" if status == "CLOSED" else None,
        "quantity": "1",
        "realizedPnl": str(pnl),
        "fees": fees,
        "payload": {"riskAmount": risk},
        "createdAt": 1,
        "updatedAt": 2,
    }


def test_trade_metrics_are_fee_and_r_multiple_aware():
    rows = [
        trade("trade_win_0001", "100", fees="10", risk="50"),
        trade(
            "trade_loss_0002",
            "-50",
            side="Sell",
            entry=1_800_000_300_000,
            exit_time=1_800_000_600_000,
            fees="8",
            risk="50",
        ),
        trade(
            "trade_open_0003",
            "0",
            status="OPEN",
            entry=1_800_000_600_000,
            fees="5",
        ),
    ]
    result = analytics.calculate_trade_metrics(rows, SESSION)
    assert result["totalTrades"] == 3
    assert result["closedTrades"] == 2
    assert result["openTrades"] == 1
    assert result["winningTrades"] == 1
    assert result["losingTrades"] == 1
    assert result["winRatePct"] == "50.0000"
    assert result["grossProfit"] == "100.00000000"
    assert result["grossLoss"] == "50.00000000"
    assert result["netRealizedPnl"] == "50.00000000"
    assert result["feesPaid"] == "23.00000000"
    assert result["profitFactor"] == "2.0000"
    assert result["expectancy"] == "25.00000000"
    assert result["totalR"] == "1.0000"
    assert result["averageR"] == "0.5000"
    assert result["netPnl"] == "45.00000000"


def test_profit_factor_truthfully_reports_no_loss_denominator():
    result = analytics.calculate_trade_metrics(
        [trade("trade_win_0001", "25")], SESSION
    )
    assert result["profitFactor"] is None
    assert result["profitFactorStatus"] == "no_losses"


def test_drawdown_uses_initial_equity_and_peak_to_trough_path():
    result = analytics.calculate_drawdown(
        "1000",
        [
            {"equity": "1100", "candleOpenTime": 100},
            {"equity": "1020", "candleOpenTime": 200},
            {"equity": "1060", "candleOpenTime": 300},
        ],
        initial_time=0,
    )
    assert result["maxDrawdown"] == "80.00000000"
    assert result["maxDrawdownPct"] == "7.2727"
    assert result["maxDrawdownPeakTime"] == 100
    assert result["maxDrawdownTroughTime"] == 200
    assert result["currentDrawdown"] == "40.00000000"
    assert result["highWaterEquity"] == "1100.00000000"


def test_equity_sampling_honors_limit_and_preserves_final_point():
    assert analytics.equity_sample_indexes(5, 2) == {0, 4}
    indexes = analytics.equity_sample_indexes(1000, 200)
    assert len(indexes) <= 200
    assert 0 in indexes
    assert 999 in indexes


def test_journal_query_is_bounded_and_filterable():
    query = analytics.normalize_journal_query(
        {
            "limit": "25",
            "direction": "asc",
            "cursorSequence": "10",
            "category": "trade",
            "includePayload": "false",
            "includeTrades": "true",
            "tradeStatus": "closed",
            "tradeLimit": "12",
        }
    )
    assert query == {
        "limit": 25,
        "direction": "asc",
        "cursorSequence": 10,
        "eventType": None,
        "category": "trade",
        "includePayload": False,
        "includeTrades": True,
        "tradeStatus": "CLOSED",
        "tradeLimit": 12,
    }
    for invalid in (0, 201, "bad"):
        with pytest.raises(analytics.ReplayAnalyticsValidationError):
            analytics.normalize_journal_query({"limit": invalid})
    with pytest.raises(analytics.ReplayAnalyticsValidationError, match="category"):
        analytics.normalize_journal_query({"category": "orders"})


def test_performance_query_bounds_equity_sampling():
    assert analytics.normalize_performance_query({}) == {
        "includeEquityCurve": True,
        "curveLimit": 200,
    }
    assert analytics.normalize_performance_query(
        {"includeEquityCurve": "0", "curveLimit": "500"}
    ) == {"includeEquityCurve": False, "curveLimit": 500}
    with pytest.raises(analytics.ReplayAnalyticsValidationError):
        analytics.normalize_performance_query({"curveLimit": 1})


def test_route_contract_and_install_decorate_session_capabilities():
    assert analytics.is_get_path(
        "/api/replay/sessions/replay_test_0001/performance"
    )
    assert analytics.is_get_path(
        "/api/replay/sessions/replay_test_0001/journal"
    )
    assert not analytics.is_get_path("/api/replay/sessions/replay_test_0001")

    class SessionService:
        def get(self, session_id):
            return {"ok": True}

        def list(self, **kwargs):
            return {"ok": True}

    core = SimpleNamespace(
        _durable_state_store=object(), _replay_session_service=SessionService()
    )
    service = analytics.install(core)
    assert isinstance(service, analytics.ReplayPerformanceJournalService)
    assert core._replay_session_service.get("replay_test_0001")[
        "performanceSummaryImplemented"
    ] is True
    assert core._replay_session_service.list()["replayJournalImplemented"] is True


def test_money_quantization_remains_decimal_not_float():
    result = analytics.calculate_trade_metrics(
        [trade("trade_small_0001", Decimal("0.123456789"), fees="0.000000019")],
        {**SESSION, "balance": "1000.123456789", "equity": "1000.123456789"},
    )
    assert result["netRealizedPnl"] == "0.12345678"
    assert result["feesPaid"] == "0.00000001"
    assert analytics.calculate_trade_metrics([], {**SESSION, "balance": "1000", "equity": "1000"})[
        "netPnl"
    ] == "0.00000000"
