from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from backend.live_execution_storage import LIVE_EXECUTION_MIGRATION
from backend.postgres_state_store import MIGRATIONS, PostgresStateStore
from backend.replay_step_repository import REPLAY_STEP_SCHEMA_VERSION
from backend.replay_storage import (
    REPLAY_SCHEMA_VERSION,
    ReplayStorageMixin,
    ReplayStorageValidationError,
    normalize_replay_candle,
    normalize_replay_session,
    normalize_replay_trade,
)


def test_replay_schema_is_versioned_after_existing_runtime_schema():
    versions = [version for version, _ in MIGRATIONS]

    assert versions == sorted(set(versions))
    assert REPLAY_SCHEMA_VERSION == 2
    assert REPLAY_STEP_SCHEMA_VERSION == 3
    assert REPLAY_SCHEMA_VERSION in versions
    assert REPLAY_STEP_SCHEMA_VERSION in versions
    assert LIVE_EXECUTION_MIGRATION in MIGRATIONS
    assert versions.index(REPLAY_SCHEMA_VERSION) < versions.index(REPLAY_STEP_SCHEMA_VERSION)
    assert versions.index(REPLAY_STEP_SCHEMA_VERSION) < versions.index(LIVE_EXECUTION_MIGRATION[0])


def test_replay_migration_creates_all_required_tables_and_uniqueness_contracts():
    statements = dict(MIGRATIONS)[REPLAY_SCHEMA_VERSION]
    sql = "\n".join(statements).lower()

    assert "create table if not exists replay_candles" in sql
    assert "primary key(symbol,timeframe,open_time)" in sql
    assert "create table if not exists replay_sessions" in sql
    assert "create table if not exists replay_events" in sql
    assert "unique(session_id,sequence_no)" in sql
    assert "create table if not exists replay_trades" in sql
    assert "primary key(session_id,trade_id)" in sql
    assert "references replay_sessions(session_id) on delete cascade" in sql


def test_replay_step_migration_persists_idempotency_and_immutable_candle_snapshots():
    statements = dict(MIGRATIONS)[REPLAY_STEP_SCHEMA_VERSION]
    sql = "\n".join(statements).lower()

    assert "create table if not exists replay_step_requests" in sql
    assert "primary key(session_id,request_id)" in sql
    assert "request_payload jsonb not null" in sql
    assert "response_payload jsonb not null" in sql
    assert "create table if not exists replay_session_candles" in sql
    assert "primary key(session_id,open_time)" in sql
    assert "snapshotted_at bigint not null" in sql
    assert "references replay_sessions(session_id) on delete cascade" in sql


def test_replay_store_extends_canonical_postgres_store_without_exchange_adapter():
    assert issubclass(PostgresStateStore, ReplayStorageMixin)

    method_names = set(dir(ReplayStorageMixin))
    assert "upsert_replay_candles" in method_names
    assert "create_replay_session" in method_names
    assert "append_replay_event" in method_names
    assert "upsert_replay_trade" in method_names
    assert "place_order" not in method_names
    assert "submit_order" not in method_names


def test_trade_upsert_is_scoped_to_session_and_session_updates_lock_bounds():
    trade_source = inspect.getsource(ReplayStorageMixin.upsert_replay_trade)
    update_source = inspect.getsource(ReplayStorageMixin.update_replay_session_state)

    assert "ON CONFLICT(session_id,trade_id)" in trade_source
    assert "SELECT start_time,end_time,summary" in update_source
    assert "FOR UPDATE" in update_source
    assert "summary is None" in update_source


def test_candle_normalization_accepts_valid_closed_ohlcv_data():
    candle = normalize_replay_candle(
        {
            "symbol": "btcusdt",
            "interval": "5",
            "time": 1_785_000_000_000,
            "open": "100.10",
            "high": "103.00",
            "low": "99.50",
            "close": "102.25",
            "volume": "250.5",
            "turnover": "25431.125",
        }
    )

    assert candle["symbol"] == "BTCUSDT"
    assert candle["timeframe"] == "5"
    assert candle["open_time"] == 1_785_000_000_000
    assert candle["close_price"] == Decimal("102.25")
    assert candle["source"] == "bybit_public_kline"


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"interval": "1"}, "timeframe"),
        ({"symbol": "BTCUSD"}, "USDT"),
        ({"high": "99"}, "high"),
        ({"low": "103"}, "low"),
        ({"volume": "-1"}, "negative"),
    ],
)
def test_candle_normalization_rejects_invalid_market_data(patch, message):
    payload = {
        "symbol": "BTCUSDT",
        "interval": "5",
        "time": 1_785_000_000_000,
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": "10",
    }
    payload.update(patch)

    with pytest.raises(ReplayStorageValidationError, match=message):
        normalize_replay_candle(payload)


def test_session_normalization_locks_range_balance_and_simulation_configuration():
    session = normalize_replay_session(
        {
            "sessionId": "replay_20260729_001",
            "symbol": "ETHUSDT",
            "timeframe": "15",
            "startTime": 1_785_000_000_000,
            "endTime": 1_785_086_400_000,
            "initialBalance": "1000",
            "strategyMode": "conservative",
            "config": {"executionMode": "simulated_only", "feeBps": 5.5},
        }
    )

    assert session["status"] == "READY"
    assert session["balance"] == Decimal("1000")
    assert session["equity"] == Decimal("1000")
    assert session["cursor_time"] is None
    assert session["config"]["executionMode"] == "simulated_only"


def test_session_rejects_reversed_range_and_out_of_range_cursor():
    base = {
        "sessionId": "replay_20260729_002",
        "symbol": "BTCUSDT",
        "timeframe": "60",
        "startTime": 2_000,
        "endTime": 3_000,
        "initialBalance": "1000",
    }

    with pytest.raises(ReplayStorageValidationError, match="after start_time"):
        normalize_replay_session({**base, "endTime": 1_000})

    with pytest.raises(ReplayStorageValidationError, match="inside the session range"):
        normalize_replay_session({**base, "cursorTime": 4_000})


def test_trade_normalization_supports_simulated_open_and_closed_lifecycle():
    opened = normalize_replay_trade(
        {
            "tradeId": "trade_20260729_001",
            "sessionId": "replay_20260729_001",
            "symbol": "BTCUSDT",
            "side": "buy",
            "status": "open",
            "entryTime": 1_785_000_000_000,
            "entryPrice": "100",
            "quantity": "0.5",
        }
    )
    closed = normalize_replay_trade(
        {
            **opened,
            "trade_id": opened["trade_id"],
            "session_id": opened["session_id"],
            "entry_time": opened["entry_time"],
            "entry_price": opened["entry_price"],
            "status": "CLOSED",
            "exit_time": 1_785_000_300_000,
            "exit_price": "102",
            "realized_pnl": "1",
            "fees": "0.05",
        }
    )

    assert opened["side"] == "Buy"
    assert opened["status"] == "OPEN"
    assert closed["status"] == "CLOSED"
    assert closed["realized_pnl"] == Decimal("1")


def test_closed_trade_requires_exit_evidence():
    with pytest.raises(ReplayStorageValidationError, match="require exit_time and exit_price"):
        normalize_replay_trade(
            {
                "tradeId": "trade_20260729_002",
                "sessionId": "replay_20260729_001",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "status": "CLOSED",
                "entryTime": 1_785_000_000_000,
                "entryPrice": "100",
                "quantity": "0.5",
            }
        )
