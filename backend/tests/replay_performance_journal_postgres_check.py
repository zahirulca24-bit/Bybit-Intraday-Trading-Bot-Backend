from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from backend.postgres_state_store import PostgresStateStore
from backend.replay_performance_journal import ReplayPerformanceJournalService

START = 1_800_000_000_000
INTERVAL = 300_000


def trade(
    session_id,
    trade_id,
    *,
    side,
    entry_index,
    exit_index,
    pnl,
    fees,
    risk,
    status="CLOSED",
):
    entry_time = START + entry_index * INTERVAL
    exit_time = START + exit_index * INTERVAL if status == "CLOSED" else None
    return {
        "trade_id": trade_id,
        "session_id": session_id,
        "symbol": "BTCUSDT",
        "side": side,
        "status": status,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": "100",
        "exit_price": "110" if status == "CLOSED" else None,
        "quantity": "1",
        "realized_pnl": pnl,
        "fees": fees,
        "payload": {
            "riskAmount": risk,
            "stopLoss": "95",
            "takeProfit": "110",
            "exitReason": "take_profit" if Decimal(str(pnl)) > 0 else "stop_loss",
        },
    }


def main():
    store = PostgresStateStore()
    assert store.status()["ok"] is True
    session_id = f"replay_{uuid4().hex}"
    created = store.create_replay_session(
        {
            "session_id": session_id,
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "status": "COMPLETED",
            "start_time": START,
            "end_time": START + 3 * INTERVAL,
            "cursor_time": START + 3 * INTERVAL,
            "initial_balance": "1000",
            "balance": "1045",
            "equity": "1060",
            "strategy_mode": "balanced",
            "config": {
                "runtimeMode": "historical_replay",
                "executionMode": "simulated_only",
                "externalExecutionAllowed": False,
            },
            "summary": {"executionSimulated": True},
        }
    )
    assert created["created"] is True

    store.upsert_replay_trade(
        trade(
            session_id,
            "trade_win_0001",
            side="Buy",
            entry_index=0,
            exit_index=1,
            pnl="100",
            fees="10",
            risk="50",
        )
    )
    store.upsert_replay_trade(
        trade(
            session_id,
            "trade_loss_0002",
            side="Sell",
            entry_index=1,
            exit_index=2,
            pnl="-50",
            fees="8",
            risk="50",
        )
    )
    store.upsert_replay_trade(
        trade(
            session_id,
            "trade_open_0003",
            side="Buy",
            entry_index=2,
            exit_index=3,
            pnl="0",
            fees="5",
            risk="50",
            status="OPEN",
        )
    )

    events = [
        (0, "session.created", None, {"source": "step8_ci"}),
        (1, "trade.opened", START, {"tradeId": "trade_win_0001"}),
        (2, "pnl.marked", START, {"balance": "1000", "equity": "1000", "unrealizedPnl": "0"}),
        (3, "trade.closed", START + INTERVAL, {"tradeId": "trade_win_0001"}),
        (4, "pnl.marked", START + INTERVAL, {"balance": "1100", "equity": "1100", "unrealizedPnl": "0"}),
        (5, "trade.closed", START + 2 * INTERVAL, {"tradeId": "trade_loss_0002"}),
        (6, "pnl.marked", START + 2 * INTERVAL, {"balance": "1050", "equity": "1020", "unrealizedPnl": "-30"}),
        (7, "trade.opened", START + 3 * INTERVAL, {"tradeId": "trade_open_0003"}),
        (8, "pnl.marked", START + 3 * INTERVAL, {"balance": "1045", "equity": "1060", "unrealizedPnl": "15"}),
        (9, "execution.completed", START + 3 * INTERVAL, {"requestId": "step8_ci_request"}),
    ]
    for sequence, event_type, candle_time, payload in events:
        assert store.append_replay_event(
            session_id,
            sequence,
            event_type,
            payload,
            candle_open_time=candle_time,
        ) is True

    service = ReplayPerformanceJournalService(store)
    performance = service.performance(
        session_id, include_equity_curve=True, curve_limit=3
    )
    metrics = performance["metrics"]
    assert performance["ok"] is True
    assert performance["isFinal"] is False  # one open trade remains
    assert metrics["closedTrades"] == 2
    assert metrics["openTrades"] == 1
    assert metrics["winRatePct"] == "50.0000"
    assert metrics["grossProfit"] == "100.00000000"
    assert metrics["grossLoss"] == "50.00000000"
    assert metrics["netRealizedPnl"] == "50.00000000"
    assert metrics["feesPaid"] == "23.00000000"
    assert metrics["profitFactor"] == "2.0000"
    assert metrics["expectancy"] == "25.00000000"
    assert metrics["totalR"] == "1.0000"
    assert metrics["averageR"] == "0.5000"
    assert metrics["maxDrawdown"] == "80.00000000"
    assert metrics["maxDrawdownPct"] == "7.2727"
    assert metrics["netPnl"] == "45.00000000"
    assert metrics["equityPnl"] == "60.00000000"
    assert performance["equityCurveMeta"]["totalMarks"] == 4
    assert performance["equityCurveMeta"]["returnedPoints"] <= 4
    assert performance["externalExecutionAllowed"] is False

    latest = service.journal(
        session_id,
        {
            "limit": 3,
            "direction": "desc",
            "includeTrades": True,
            "tradeStatus": "CLOSED",
            "tradeLimit": 10,
        },
    )
    assert [entry["sequenceNo"] for entry in latest["entries"]] == [9, 8, 7]
    assert latest["pagination"]["hasMore"] is True
    assert latest["pagination"]["nextCursorSequence"] == 7
    assert len(latest["trades"]) == 2
    assert all(row["status"] == "CLOSED" for row in latest["trades"])
    assert latest["journalSummary"] == {
        "totalEvents": 10,
        "firstSequence": 0,
        "lastSequence": 9,
        "totalTrades": 3,
        "openTrades": 1,
        "closedTrades": 2,
        "cancelledTrades": 0,
    }

    older = service.journal(
        session_id,
        {
            "limit": 3,
            "direction": "desc",
            "cursorSequence": latest["pagination"]["nextCursorSequence"],
            "category": "pnl",
            "includePayload": False,
            "includeTrades": False,
        },
    )
    assert [entry["sequenceNo"] for entry in older["entries"]] == [6, 4, 2]
    assert all("payload" not in entry for entry in older["entries"])
    assert older["trades"] == []

    ascending = service.journal(
        session_id,
        {
            "limit": 2,
            "direction": "asc",
            "cursorSequence": 3,
            "eventType": "pnl.marked",
            "includeTrades": False,
        },
    )
    assert [entry["sequenceNo"] for entry in ascending["entries"]] == [4, 6]
    assert ascending["pagination"]["nextCursorSequence"] == 6


if __name__ == "__main__":
    main()
