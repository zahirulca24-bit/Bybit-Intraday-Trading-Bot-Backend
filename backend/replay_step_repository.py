"""Atomic PostgreSQL repository for candle-by-candle Historical Replay.

The repository advances only session-frozen historical candles. It contains no
exchange client, authenticated endpoint, order submission, position management,
strategy signal, fill simulation, or PnL calculation capability.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

try:
    from .replay_storage import _jsonb, _normalized_identifier, _session_row
except ImportError:
    from replay_storage import _jsonb, _normalized_identifier, _session_row


INTERVAL_MS = {"5": 300_000, "15": 900_000, "60": 3_600_000}
MAX_STEP_CANDLES = 100
REPLAY_STEP_SCHEMA_VERSION = 3
REPLAY_STEP_MIGRATION: tuple[int, tuple[str, ...]] = (
    REPLAY_STEP_SCHEMA_VERSION,
    (
        "CREATE TABLE IF NOT EXISTS replay_step_requests ("
        "session_id TEXT NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE, "
        "request_id TEXT NOT NULL, request_payload JSONB NOT NULL, response_payload JSONB NOT NULL, "
        "created_at BIGINT NOT NULL, PRIMARY KEY(session_id,request_id))",
        "CREATE INDEX IF NOT EXISTS ix_replay_step_requests_created "
        "ON replay_step_requests(session_id,created_at DESC)",
        "CREATE TABLE IF NOT EXISTS replay_session_candles ("
        "session_id TEXT NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE, "
        "open_time BIGINT NOT NULL, open_price NUMERIC(38,18) NOT NULL, "
        "high_price NUMERIC(38,18) NOT NULL, low_price NUMERIC(38,18) NOT NULL, "
        "close_price NUMERIC(38,18) NOT NULL, volume NUMERIC(38,18) NOT NULL, "
        "turnover NUMERIC(38,18), source TEXT NOT NULL, snapshotted_at BIGINT NOT NULL, "
        "PRIMARY KEY(session_id,open_time))",
        "CREATE INDEX IF NOT EXISTS ix_replay_session_candles_range "
        "ON replay_session_candles(session_id,open_time)",
    ),
)
_STEPPABLE_STATUSES = frozenset({"READY", "PAUSED"})


class ReplayStepRepositoryError(RuntimeError):
    """Raised when the persistent replay step repository is unavailable."""


class ReplayStepNotFoundError(ReplayStepRepositoryError):
    """Raised when a replay session does not exist."""


class ReplayStepConflictError(ReplayStepRepositoryError):
    """Raised for stale cursors, duplicate-key conflicts, or invalid state."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class ReplayStepDataIncompleteError(ReplayStepConflictError):
    """Raised when the next expected historical candle range is not continuous."""


def require_store(store: Any) -> Any:
    """Return a healthy PostgreSQL replay store or fail closed."""

    if store is None or not hasattr(store, "lock") or not callable(getattr(store, "connect", None)):
        raise ReplayStepRepositoryError("Persistent PostgreSQL replay storage is unavailable.")
    status = getattr(store, "status", None)
    if callable(status):
        snapshot = dict(status() or {})
        if not snapshot.get("ok") or snapshot.get("degraded"):
            raise ReplayStepRepositoryError(
                snapshot.get("error") or "Persistent PostgreSQL replay storage is degraded."
            )
        if int(snapshot.get("migrationVersion") or 0) < REPLAY_STEP_SCHEMA_VERSION:
            raise ReplayStepRepositoryError(
                "Replay candle-step database migration is not applied."
            )
    return store


def snapshot_session_candles(
    cursor: Any,
    session: Mapping[str, Any],
    *,
    snapshotted_at: int | None = None,
) -> int:
    """Copy current source candles once; existing snapshot rows are never overwritten."""

    session_id = _normalized_identifier(
        session.get("sessionId", session.get("session_id")), "session_id"
    )
    symbol = str(session.get("symbol") or "")
    timeframe = str(session.get("timeframe") or "")
    start_time = int(session.get("startTime", session.get("start_time")))
    end_time = int(session.get("endTime", session.get("end_time")))
    frozen_at = int(snapshotted_at if snapshotted_at is not None else time.time())
    cursor.execute(
        "INSERT INTO replay_session_candles("
        "session_id,open_time,open_price,high_price,low_price,close_price,volume,turnover,"
        "source,snapshotted_at) "
        "SELECT %s,open_time,open_price,high_price,low_price,close_price,volume,turnover,"
        "source,%s FROM replay_candles WHERE symbol=%s AND timeframe=%s "
        "AND open_time BETWEEN %s AND %s ORDER BY open_time ASC "
        "ON CONFLICT(session_id,open_time) DO NOTHING",
        (session_id, frozen_at, symbol, timeframe, start_time, end_time),
    )
    return int(cursor.rowcount or 0)


def _ensure_session_snapshot(
    cursor: Any,
    session: Mapping[str, Any],
    *,
    snapshotted_at: int,
) -> int:
    """Backfill pre-Step-5 sessions once, then keep their candle stream immutable."""

    session_id = str(session["sessionId"])
    cursor.execute(
        "SELECT COUNT(*) FROM replay_session_candles WHERE session_id=%s",
        (session_id,),
    )
    count = int(cursor.fetchone()[0] or 0)
    if count == 0:
        snapshot_session_candles(
            cursor,
            session,
            snapshotted_at=snapshotted_at,
        )
        cursor.execute(
            "SELECT COUNT(*) FROM replay_session_candles WHERE session_id=%s",
            (session_id,),
        )
        count = int(cursor.fetchone()[0] or 0)
    return count


def _candle_row(symbol: str, timeframe: str, row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "openTime": int(row[0]),
        "open": str(row[1]),
        "high": str(row[2]),
        "low": str(row[3]),
        "close": str(row[4]),
        "volume": str(row[5]),
        "turnover": str(row[6]) if row[6] is not None else None,
        "source": row[7],
    }


def _stored_step_request(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "steps": int(request["steps"]),
        "expectedCursorTime": request.get("expectedCursorTime"),
    }


def _idempotent_response(
    stored_request: Any,
    stored_response: Any,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(stored_request, Mapping) or dict(stored_request) != _stored_step_request(request):
        raise ReplayStepConflictError(
            "requestId already exists with different replay step parameters.",
            {"requestId": request["requestId"]},
        )
    if not isinstance(stored_response, Mapping):
        raise ReplayStepRepositoryError("Stored replay step response is invalid.")
    result = dict(stored_response)
    result["idempotent"] = True
    return result


def advance_session(store: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically advance one replay session across a bounded continuous candle batch."""

    store = require_store(store)
    session_id = _normalized_identifier(request.get("sessionId"), "session_id")
    request_id = _normalized_identifier(request.get("requestId"), "request_id")
    requested_steps = int(request["steps"])
    if not 1 <= requested_steps <= MAX_STEP_CANDLES:
        raise ReplayStepConflictError(
            f"steps must be between 1 and {MAX_STEP_CANDLES}."
        )
    expected_cursor = request.get("expectedCursorTime")
    now = int(time.time())

    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
                "FROM replay_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ReplayStepNotFoundError("Replay session was not found.")
            session = _session_row(row)
            if session is None:
                raise ReplayStepRepositoryError("Replay session state is invalid.")

            cur.execute(
                "SELECT request_payload,response_payload FROM replay_step_requests "
                "WHERE session_id=%s AND request_id=%s",
                (session_id, request_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                return _idempotent_response(existing[0], existing[1], request)

            status = str(session["status"]).upper()
            if status not in _STEPPABLE_STATUSES:
                raise ReplayStepConflictError(
                    f"Replay session status {status} cannot be stepped.",
                    {"sessionId": session_id, "status": status},
                )

            actual_cursor = session.get("cursorTime")
            if expected_cursor != actual_cursor:
                raise ReplayStepConflictError(
                    "Replay cursor changed; refresh the session before stepping again.",
                    {
                        "sessionId": session_id,
                        "expectedCursorTime": expected_cursor,
                        "actualCursorTime": actual_cursor,
                    },
                )

            timeframe = str(session["timeframe"])
            interval_ms = INTERVAL_MS.get(timeframe)
            if interval_ms is None:
                raise ReplayStepRepositoryError("Replay session timeframe is unsupported.")
            start_time = int(session["startTime"])
            end_time = int(session["endTime"])
            snapshot_count = _ensure_session_snapshot(
                cur,
                session,
                snapshotted_at=now,
            )
            expected_snapshot_count = ((end_time - start_time) // interval_ms) + 1
            if snapshot_count != expected_snapshot_count:
                raise ReplayStepDataIncompleteError(
                    "Replay session candle snapshot is incomplete.",
                    {
                        "sessionId": session_id,
                        "expectedCandles": expected_snapshot_count,
                        "snapshottedCandles": snapshot_count,
                        "startTime": start_time,
                        "endTime": end_time,
                    },
                )

            next_time = start_time if actual_cursor is None else int(actual_cursor) + interval_ms
            if next_time > end_time:
                raise ReplayStepConflictError(
                    "Replay session has no remaining candles.",
                    {"sessionId": session_id, "cursorTime": actual_cursor, "endTime": end_time},
                )

            remaining_including_next = ((end_time - next_time) // interval_ms) + 1
            process_count = min(requested_steps, remaining_including_next)
            last_expected_time = next_time + ((process_count - 1) * interval_ms)
            cur.execute(
                "SELECT open_time,open_price,high_price,low_price,close_price,volume,turnover,source "
                "FROM replay_session_candles WHERE session_id=%s "
                "AND open_time BETWEEN %s AND %s ORDER BY open_time ASC",
                (session_id, next_time, last_expected_time),
            )
            candle_rows = cur.fetchall()
            candles = [
                _candle_row(str(session["symbol"]), timeframe, candle_row)
                for candle_row in candle_rows
            ]
            expected_times = [next_time + (index * interval_ms) for index in range(process_count)]
            actual_times = [int(candle["openTime"]) for candle in candles]
            if actual_times != expected_times:
                actual_set = set(actual_times)
                missing = [timestamp for timestamp in expected_times if timestamp not in actual_set]
                raise ReplayStepDataIncompleteError(
                    "Replay candle snapshot is incomplete at the current cursor.",
                    {
                        "sessionId": session_id,
                        "nextCandleOpenTime": next_time,
                        "lastExpectedOpenTime": last_expected_time,
                        "expectedCandles": process_count,
                        "availableCandles": len(candles),
                        "missingOpenTimes": missing[:20],
                    },
                )

            cur.execute(
                "SELECT COALESCE(MAX(sequence_no),-1) FROM replay_events WHERE session_id=%s",
                (session_id,),
            )
            previous_sequence = int(cur.fetchone()[0])
            started_sequence = previous_sequence + 1
            completed_sequence = started_sequence + len(candles) + 1
            step_request = _stored_step_request(request)
            cur.execute(
                "INSERT INTO replay_events("
                "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                ") VALUES(%s,%s,'step.started',NULL,%s,%s)",
                (
                    session_id,
                    started_sequence,
                    _jsonb(
                        {
                            "requestId": request_id,
                            "request": step_request,
                            "previousCursorTime": actual_cursor,
                            "requestedSteps": requested_steps,
                            "candleSource": "session_snapshot",
                        }
                    ),
                    now,
                ),
            )

            for index, candle in enumerate(candles, start=1):
                cur.execute(
                    "INSERT INTO replay_events("
                    "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                    ") VALUES(%s,%s,'candle.advanced',%s,%s,%s)",
                    (
                        session_id,
                        started_sequence + index,
                        candle["openTime"],
                        _jsonb(
                            {
                                "requestId": request_id,
                                "stepIndex": index,
                                "candle": candle,
                                "candleSource": "session_snapshot",
                                "strategyEvaluated": False,
                                "executionSimulated": False,
                            }
                        ),
                        now,
                    ),
                )

            new_cursor = int(candles[-1]["openTime"])
            remaining_candles = (end_time - new_cursor) // interval_ms
            final_status = "COMPLETED" if remaining_candles == 0 else "PAUSED"
            total_processed = ((new_cursor - start_time) // interval_ms) + 1
            summary = dict(session.get("summary") or {})
            summary.update(
                {
                    "processedCandles": total_processed,
                    "remainingCandles": remaining_candles,
                    "lastCandleOpenTime": new_cursor,
                    "lastStepRequestId": request_id,
                    "stepEngineVersion": 1,
                    "candleSnapshotFrozen": True,
                    "strategyEvaluated": False,
                    "executionSimulated": False,
                }
            )
            cur.execute(
                "UPDATE replay_sessions SET status=%s,cursor_time=%s,summary=%s,updated_at=%s "
                "WHERE session_id=%s RETURNING "
                "session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at",
                (final_status, new_cursor, _jsonb(summary), now, session_id),
            )
            updated_row = cur.fetchone()
            updated_session = _session_row(updated_row)
            if updated_session is None:
                raise ReplayStepRepositoryError("Replay step did not return persistent session state.")

            response = {
                "ok": True,
                "idempotent": False,
                "requestId": request_id,
                "requestedSteps": requested_steps,
                "processedSteps": len(candles),
                "previousCursorTime": actual_cursor,
                "cursorTime": new_cursor,
                "remainingCandles": remaining_candles,
                "completed": final_status == "COMPLETED",
                "session": updated_session,
                "candles": candles,
                "candleSource": "session_snapshot",
                "events": {
                    "startedSequence": started_sequence,
                    "completedSequence": completed_sequence,
                },
                "strategyEvaluated": False,
                "executionSimulated": False,
                "externalExecutionAllowed": False,
            }
            cur.execute(
                "INSERT INTO replay_events("
                "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                ") VALUES(%s,%s,'step.completed',%s,%s,%s)",
                (
                    session_id,
                    completed_sequence,
                    new_cursor,
                    _jsonb(
                        {
                            "requestId": request_id,
                            "request": step_request,
                            "response": response,
                        }
                    ),
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO replay_step_requests("
                "session_id,request_id,request_payload,response_payload,created_at"
                ") VALUES(%s,%s,%s,%s,%s)",
                (
                    session_id,
                    request_id,
                    _jsonb(step_request),
                    _jsonb(response),
                    now,
                ),
            )
        db.commit()
    return response
