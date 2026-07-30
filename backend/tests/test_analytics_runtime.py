from __future__ import annotations

import math

try:
    from backend.analytics_runtime import build_analytics_snapshot, normalize_closed_trade
except ImportError:
    from analytics_runtime import build_analytics_snapshot, normalize_closed_trade


def _trade(identity: str, pnl: float, closed_at: int, symbol: str = "BTCUSDT", side: str = "LONG"):
    return {
        "id": identity,
        "orderId": identity,
        "symbol": symbol,
        "side": side,
        "positionSide": side,
        "closingSide": "SELL" if side == "LONG" else "BUY",
        "closedPnl": pnl,
        "closedSize": 1.0,
        "avgEntryPrice": 100.0,
        "avgExitPrice": 101.0,
        "leverage": 3.0,
        "closedAt": closed_at,
        "strategy": None,
        "strategyAttribution": "UNATTRIBUTED",
    }


def test_normalize_closed_trade_preserves_close_side_and_position_truth():
    row = normalize_closed_trade({
        "orderId": "order-1",
        "symbol": "ethusdt",
        "side": "Sell",
        "closedPnl": "12.50",
        "closedSize": "2",
        "avgEntryPrice": "100",
        "avgExitPrice": "106.25",
        "updatedTime": "1700000000000",
    })
    assert row["id"] == "order-1"
    assert row["symbol"] == "ETHUSDT"
    assert row["closingSide"] == "SELL"
    assert row["side"] == "LONG"
    assert row["positionSide"] == "LONG"
    assert row["closedPnl"] == 12.5
    assert row["closedAt"] == 1700000000000
    assert row["strategyAttribution"] == "UNATTRIBUTED"


def test_buy_close_is_reported_as_short_position():
    row = normalize_closed_trade({
        "orderId": "order-2",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "closedPnl": "5.0",
        "updatedTime": "1700000001000",
    })
    assert row["closingSide"] == "BUY"
    assert row["side"] == "SHORT"
    assert row["positionSide"] == "SHORT"


def test_summary_profit_factor_expectancy_and_drawdown():
    rows = [
        _trade("1", 10, 1),
        _trade("2", -5, 2),
        _trade("3", 20, 3, "ETHUSDT", "SHORT"),
        _trade("4", -10, 4, "ETHUSDT", "SHORT"),
        _trade("5", 0, 5),
    ]
    snapshot = build_analytics_snapshot(rows, max_rows=200)
    summary = snapshot["summary"]

    assert summary["totalTrades"] == 5
    assert summary["wins"] == 2
    assert summary["losses"] == 2
    assert summary["breakeven"] == 1
    assert summary["winRatePct"] == 40.0
    assert summary["netPnl"] == 15.0
    assert summary["grossProfit"] == 30.0
    assert summary["grossLoss"] == 15.0
    assert summary["profitFactor"] == 2.0
    assert summary["expectancy"] == 3.0
    assert summary["maxDrawdown"] == 10.0
    assert snapshot["drawdown"]["curve"][-1]["cumulativePnl"] == 15.0
    assert snapshot["metadata"]["source"] == "BYBIT_DEMO_CLOSED_PNL"
    assert snapshot["metadata"]["lookbackDays"] == 7
    assert snapshot["metadata"]["windowSource"] == "BYBIT_DEFAULT_7_DAY_WINDOW"
    assert snapshot["metadata"]["windowEnd"] > snapshot["metadata"]["windowStart"]


def test_breakdown_is_symbol_and_position_side_truth_not_fake_strategy_data():
    rows = [
        _trade("1", 8, 1, "BTCUSDT", "LONG"),
        _trade("2", -2, 2, "BTCUSDT", "LONG"),
        _trade("3", 5, 3, "ETHUSDT", "SHORT"),
    ]
    snapshot = build_analytics_snapshot(rows)
    by_symbol = {row["label"]: row for row in snapshot["breakdown"]["bySymbol"]}
    by_side = {row["label"]: row for row in snapshot["breakdown"]["bySide"]}

    assert by_symbol["BTCUSDT"]["totalTrades"] == 2
    assert by_symbol["BTCUSDT"]["netPnl"] == 6.0
    assert by_symbol["ETHUSDT"]["winRatePct"] == 100.0
    assert by_side["LONG"]["wins"] == 1
    assert by_side["SHORT"]["losses"] == 0
    assert snapshot["breakdown"]["unattributedTrades"] == 3
    assert snapshot["metadata"]["strategyAttribution"] == "UNAVAILABLE_FOR_LEGACY_EXCHANGE_ROWS"


def test_empty_exchange_history_returns_truthful_zero_state():
    snapshot = build_analytics_snapshot([])
    assert snapshot["summary"]["totalTrades"] == 0
    assert snapshot["summary"]["netPnl"] == 0.0
    assert snapshot["summary"]["profitFactor"] is None
    assert snapshot["summary"]["pnlSharpe"] is None
    assert snapshot["drawdown"]["curve"] == []
    assert snapshot["metadata"]["truthfulEmptyState"] is True


def test_trade_level_sharpe_is_finite_when_variance_exists():
    snapshot = build_analytics_snapshot([
        _trade("1", 1, 1),
        _trade("2", 2, 2),
        _trade("3", -1, 3),
    ])
    assert snapshot["summary"]["pnlSharpe"] is not None
    assert math.isfinite(snapshot["summary"]["pnlSharpe"])
