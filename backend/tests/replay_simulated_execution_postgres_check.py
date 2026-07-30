from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from backend import replay_simulated_execution, replay_strategy_risk
from backend.postgres_state_store import PostgresStateStore
from backend.replay_engine import (
    CandleReplayEngine,
    ReplayEngineConflictError,
    ReplayEngineStoreError,
    ReplayEngineValidationError,
)
from backend.replay_session_repository import create_session


INTERVAL = 300_000
START = 1_800_000_000_000


def decision(history, session):
    close = Decimal(str(history[-1]["close"]))
    return {
        "evaluated": True,
        "signal": "Buy",
        "grade": "A+",
        "eligible": True,
        "reason": "deterministic Step 7 integration fixture",
        "risk": {
            "riskPct": "1.00",
            "riskAmount": "10",
            "entryPrice": str(close),
            "stopLoss": str(close - Decimal("5")),
            "takeProfit": str(close + Decimal("10")),
            "stopDistance": "5",
            "rewardRisk": "2",
            "quantity": "2",
            "sizingStatus": "candidate_only",
        },
        "indicators": {"historyCandles": len(history)},
        "executionSimulated": False,
        "externalExecutionAllowed": False,
    }


def liquidation_decision(history, session):
    result = decision(history, session)
    close = Decimal(str(history[-1]["close"]))
    result["risk"].update(
        {
            "stopLoss": str(close - Decimal("0.10")),
            "takeProfit": str(close + Decimal("0.20")),
            "stopDistance": "0.10",
            "quantity": "100",
        }
    )
    return result


def candle(symbol, index, open_price, high, low, close):
    return {
        "symbol": symbol,
        "timeframe": "5",
        "open_time": START + index * INTERVAL,
        "open": str(open_price),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": "100",
        "turnover": str(Decimal(str(close)) * Decimal("100")),
        "source": "step7_ci_fixture",
    }


def make_session(store, symbol, count, config_patch=None):
    session_id = f"replay_{uuid4().hex}"
    config = {
        "runtimeMode": "historical_replay",
        "executionMode": "simulated_only",
        "externalExecutionAllowed": False,
        "replayFeeBps": "6",
        "maxLeverage": "3",
    }
    config.update(config_patch or {})
    result = create_session(
        store,
        {
            "session_id": session_id,
            "symbol": symbol,
            "timeframe": "5",
            "status": "READY",
            "start_time": START,
            "end_time": START + (count - 1) * INTERVAL,
            "cursor_time": None,
            "initial_balance": "1000",
            "balance": "1000",
            "equity": "1000",
            "strategy_mode": "balanced",
            "config": config,
            "summary": {},
        },
        {"source": "step7_ci"},
    )
    assert result["created"] is True
    assert result["candleSnapshotCount"] == count
    return session_id


def main():
    store = PostgresStateStore()
    assert store.status()["ok"] is True
    replay_strategy_risk.evaluate = decision

    symbol = "BTCUSDT"
    rows = [
        candle(symbol, 0, 100, 101, 99, 100),
        candle(symbol, 1, 100, 111, 94, 102),
        candle(symbol, 2, 103, 105, 100, 104),
    ]
    assert store.upsert_replay_candles(rows) == 3
    session_id = make_session(store, symbol, 3)
    engine = CandleReplayEngine(store)

    first_request = {
        "sessionId": session_id,
        "requestId": "step7_open_0001",
        "steps": 1,
        "expectedCursorTime": None,
    }
    first = engine.step(first_request)
    assert first["executionEnrichmentComplete"] is True
    assert first["executionSimulated"] is True
    assert first["session"]["status"] == "PAUSED"
    assert first["execution"]["opened"] == 1
    assert first["execution"]["closed"] == 0
    assert first["execution"]["openTrades"] == 1
    assert Decimal(first["execution"]["requestFees"]) > 0
    first_trades = store.list_replay_trades(session_id)
    assert len(first_trades) == 1
    assert first_trades[0]["status"] == "OPEN"

    second = engine.step(
        {
            "sessionId": session_id,
            "requestId": "step7_close_open_0002",
            "steps": 1,
            "expectedCursorTime": first["cursorTime"],
        }
    )
    assert second["execution"]["closed"] == 1
    assert second["execution"]["opened"] == 1
    trades_after_second = store.list_replay_trades(session_id)
    assert len(trades_after_second) == 2
    first_closed = trades_after_second[0]
    assert first_closed["status"] == "CLOSED"
    assert first_closed["payload"]["exitReason"] == "stop_loss"
    assert first_closed["payload"]["sameCandleConflict"] is True
    assert Decimal(first_closed["realizedPnl"]) < 0

    final = engine.step(
        {
            "sessionId": session_id,
            "requestId": "step7_final_0003",
            "steps": 1,
            "expectedCursorTime": second["cursorTime"],
        }
    )
    assert final["completed"] is True
    assert final["session"]["status"] == "COMPLETED"
    assert final["execution"]["openTrades"] == 0
    assert final["execution"]["closedTrades"] == 2
    assert final["session"]["summary"]["executionSimulated"] is True
    assert Decimal(final["session"]["summary"]["feesPaid"]) > 0
    assert final["externalExecutionAllowed"] is False
    all_trades = store.list_replay_trades(session_id)
    assert all(trade["status"] == "CLOSED" for trade in all_trades)
    assert all_trades[1]["payload"]["exitReason"] == "session_end"

    events_before_retry = store.list_replay_events(session_id, limit=500)
    trades_before_retry = store.list_replay_trades(session_id, limit=100)
    retry = engine.step(first_request)
    assert retry["idempotent"] is True
    assert retry["executionEnrichmentComplete"] is True
    assert store.list_replay_events(session_id, limit=500) == events_before_retry
    assert store.list_replay_trades(session_id, limit=100) == trades_before_retry
    assert [event["sequenceNo"] for event in events_before_retry] == list(
        range(len(events_before_retry))
    )

    # A 3x position that gaps almost to zero is reconciled through explicit
    # limited-liability liquidation: balance, net PnL, and trade PnL all agree.
    replay_strategy_risk.evaluate = liquidation_decision
    gap_symbol = "SOLUSDT"
    gap_rows = [
        candle(gap_symbol, 0, 100, 101, 99, 100),
        candle(gap_symbol, 1, 1, 2, Decimal("0.5"), Decimal("1.5")),
    ]
    assert store.upsert_replay_candles(gap_rows) == 2
    gap_id = make_session(store, gap_symbol, 2)
    gap_engine = CandleReplayEngine(store)
    gap_open = gap_engine.step(
        {
            "sessionId": gap_id,
            "requestId": "step7_gap_open_0001",
            "steps": 1,
            "expectedCursorTime": None,
        }
    )
    gap_final = gap_engine.step(
        {
            "sessionId": gap_id,
            "requestId": "step7_gap_close_0002",
            "steps": 1,
            "expectedCursorTime": gap_open["cursorTime"],
        }
    )
    gap_trade = store.list_replay_trades(gap_id)[0]
    assert gap_trade["payload"]["limitedLiabilityApplied"] is True
    assert Decimal(gap_trade["realizedPnl"]) == Decimal("-1000")
    assert Decimal(gap_final["session"]["balance"]) == Decimal("0")
    assert Decimal(gap_final["session"]["equity"]) == Decimal("0")
    assert Decimal(gap_final["session"]["summary"]["netPnl"]) == Decimal("-1000")
    assert Decimal(gap_final["session"]["summary"]["realizedPnl"]) == Decimal("-1000")
    payload = gap_trade["payload"]
    reconciled = (
        Decimal(payload["grossPnl"])
        + Decimal(payload["liquidationAdjustment"])
        - Decimal(gap_trade["fees"])
    )
    assert reconciled == Decimal(gap_trade["realizedPnl"])

    # Invalid immutable execution assumptions fail before cursor/event mutation
    # and do not wedge the session in recovery state.
    replay_strategy_risk.evaluate = decision
    invalid_symbol = "XRPUSDT"
    invalid_rows = [
        candle(invalid_symbol, 0, 10, 11, 9, 10),
        candle(invalid_symbol, 1, 10, 11, 9, Decimal("10.5")),
    ]
    assert store.upsert_replay_candles(invalid_rows) == 2
    invalid_id = make_session(
        store, invalid_symbol, 2, {"maxLeverage": "99"}
    )
    invalid_events = store.list_replay_events(invalid_id)
    try:
        CandleReplayEngine(store).step(
            {
                "sessionId": invalid_id,
                "requestId": "step7_invalid_config_0001",
                "steps": 1,
                "expectedCursorTime": None,
            }
        )
    except ReplayEngineValidationError as exc:
        assert "Invalid replay execution configuration" in str(exc)
    else:
        raise AssertionError("invalid execution configuration advanced the replay")
    invalid_session = store.get_replay_session(invalid_id)
    assert invalid_session["status"] == "READY"
    assert invalid_session["cursorTime"] is None
    assert store.list_replay_events(invalid_id) == invalid_events

    # A retryable persistence failure blocks new request IDs, then the original
    # request resumes and clears every recovery-only marker.
    recovery_symbol = "ETHUSDT"
    recovery_rows = [
        candle(recovery_symbol, 0, 50, 51, 49, 50),
        candle(recovery_symbol, 1, 50, 52, 48, 51),
    ]
    assert store.upsert_replay_candles(recovery_rows) == 2
    recovery_id = make_session(store, recovery_symbol, 2)
    recovery_engine = CandleReplayEngine(store)
    recovery_request = {
        "sessionId": recovery_id,
        "requestId": "step7_recovery_0001",
        "steps": 1,
        "expectedCursorTime": None,
    }
    original_enrich = replay_simulated_execution.enrich_step

    def fail_once(store, result):
        raise replay_simulated_execution.ReplaySimulationError("fixture failure")

    replay_simulated_execution.enrich_step = fail_once
    try:
        recovery_engine.step(recovery_request)
    except ReplayEngineStoreError as exc:
        assert "fixture failure" in str(exc)
    else:
        raise AssertionError("simulated execution failure was accepted")
    finally:
        replay_simulated_execution.enrich_step = original_enrich

    blocked = store.get_replay_session(recovery_id)
    assert blocked["status"] == "RUNNING"
    assert blocked["summary"]["pendingFinalStatus"] == "PAUSED"
    assert blocked["summary"]["pipelineRecoveryRequired"] is True
    try:
        recovery_engine.step(
            {
                "sessionId": recovery_id,
                "requestId": "step7_wrong_retry_0002",
                "steps": 1,
                "expectedCursorTime": START,
            }
        )
    except ReplayEngineConflictError as exc:
        assert "RUNNING" in str(exc)
    else:
        raise AssertionError("new request bypassed pending replay recovery")

    recovered = recovery_engine.step(recovery_request)
    assert recovered["idempotent"] is True
    assert recovered["executionEnrichmentComplete"] is True
    assert recovered["session"]["status"] == "PAUSED"
    persisted_recovered = store.get_replay_session(recovery_id)
    for key in (
        "pendingFinalStatus",
        "pipelineRecoveryRequired",
        "pendingReplayRequestId",
    ):
        assert key not in recovered["session"]["summary"]
        assert key not in persisted_recovered["summary"]


if __name__ == "__main__":
    main()
