"""Repository operations for the Historical Replay session API.

The repository is limited to PostgreSQL replay state. It has no exchange client,
private API access, order submission, or position-management capability.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

try:
    from .replay_step_repository import snapshot_session_candles
    from .replay_storage import (
        REPLAY_SESSION_STATUSES,
        ReplayStorageValidationError,
        _jsonb,
        _normalized_identifier,
        _session_row,
        normalize_replay_session,
    )
except ImportError:
    from replay_step_repository import snapshot_session_candles
    from replay_storage import (
        REPLAY_SESSION_STATUSES,
        ReplayStorageValidationError,
        _jsonb,
        _normalized_identifier,
        _session_row,
        normalize_replay_session,
    )


class ReplaySessionRepositoryError(RuntimeError):
    """Raised when the persistent replay session repository is unavailable."""


_REQUIRED_STORE_METHODS = (
    "get_replay_session",
    "list_replay_events",
    "list_replay_trades",
)


def require_store(store: Any) -> Any:
    """Return a healthy replay store or fail closed."""

    if store is None:
        raise ReplaySessionRepositoryError(
            "Persistent PostgreSQL replay storage is unavailable."
        )
    missing = [
        name for name in _REQUIRED_STORE_METHODS if not callable(getattr(store, name, None))
    ]
    if missing or not hasattr(store, "lock") or not callable(getattr(store, "connect", None)):
        raise ReplaySessionRepositoryError(
            "Persistent PostgreSQL replay session repository is incomplete."
        )
    status = getattr(store, "status", None)
    if callable(status):
        snapshot = dict(status() or {})
        if not snapshot.get("ok") or snapshot.get("degraded"):
            raise ReplaySessionRepositoryError(
                snapshot.get("error") or "Persistent PostgreSQL replay storage is degraded."
            )
    return store


def create_session(
    store: Any,
    payload: Mapping[str, Any],
    created_event: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically create a session, immutable candle snapshot, and audit event."""

    store = require_store(store)
    session = normalize_replay_session(payload)
    now = int(time.time())
    snapshot_count = 0
    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO replay_sessions("
                "session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at"
                ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(session_id) DO NOTHING",
                (
                    session["session_id"],
                    session["symbol"],
                    session["timeframe"],
                    session["status"],
                    session["start_time"],
                    session["end_time"],
                    session["cursor_time"],
                    session["initial_balance"],
                    session["balance"],
                    session["equity"],
                    session["strategy_mode"],
                    _jsonb(session["config"]),
                    _jsonb(session["summary"]),
                    now,
                    now,
                ),
            )
            created = cur.rowcount == 1
            if created:
                snapshot_count = snapshot_session_candles(
                    cur,
                    session,
                    snapshotted_at=now,
                )
                event_payload = dict(created_event)
                event_payload.update(
                    {
                        "candleSnapshotFrozen": True,
                        "candleSnapshotCount": snapshot_count,
                    }
                )
                cur.execute(
                    "INSERT INTO replay_events("
                    "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                    ") VALUES(%s,0,'session.created',NULL,%s,%s)",
                    (session["session_id"], _jsonb(event_payload), now),
                )
            cur.execute(
                "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
                "FROM replay_sessions WHERE session_id=%s",
                (session["session_id"],),
            )
            row = cur.fetchone()
        db.commit()

    persisted = _session_row(row)
    if persisted is None:
        raise ReplaySessionRepositoryError(
            "Replay session creation did not produce persistent state."
        )
    return {
        "created": created,
        "session": persisted,
        "candleSnapshotCount": snapshot_count if created else None,
    }


def get_session(store: Any, session_id: str) -> dict[str, Any] | None:
    store = require_store(store)
    return store.get_replay_session(session_id)


def list_sessions(
    store: Any,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List sessions with server-side status filtering and a bounded result size."""

    store = require_store(store)
    bounded_limit = max(1, min(100, int(limit)))
    normalized_status = None
    if status is not None and str(status).strip():
        normalized_status = str(status).strip().upper()
        if normalized_status not in REPLAY_SESSION_STATUSES:
            raise ReplayStorageValidationError("Unsupported replay session status.")

    select = (
        "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
        "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
        "FROM replay_sessions "
    )
    params: tuple[Any, ...]
    if normalized_status is None:
        sql = select + "ORDER BY updated_at DESC,session_id DESC LIMIT %s"
        params = (bounded_limit,)
    else:
        sql = (
            select
            + "WHERE status=%s ORDER BY updated_at DESC,session_id DESC LIMIT %s"
        )
        params = (normalized_status, bounded_limit)

    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [session for row in rows if (session := _session_row(row)) is not None]


def list_events(store: Any, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    store = require_store(store)
    return store.list_replay_events(session_id, limit=max(1, min(1000, int(limit))))


def list_trades(store: Any, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    store = require_store(store)
    return store.list_replay_trades(session_id, limit=max(1, min(1000, int(limit))))


def reset_session(store: Any, session_id: str) -> dict[str, Any] | None:
    """Clear simulated activity while preserving candle snapshot and request history."""

    store = require_store(store)
    normalized_id = _normalized_identifier(session_id, "session_id")
    now = int(time.time())

    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT status,initial_balance FROM replay_sessions "
                "WHERE session_id=%s FOR UPDATE",
                (normalized_id,),
            )
            locked = cur.fetchone()
            if locked is None:
                return None
            status, initial_balance = locked
            if str(status).upper() == "RUNNING":
                raise ReplayStorageValidationError(
                    "A running replay session must be paused before reset."
                )

            cur.execute("DELETE FROM replay_trades WHERE session_id=%s", (normalized_id,))
            cur.execute("DELETE FROM replay_events WHERE session_id=%s", (normalized_id,))
            cur.execute(
                "UPDATE replay_sessions SET status='READY',cursor_time=NULL,"
                "balance=%s,equity=%s,summary=%s,updated_at=%s "
                "WHERE session_id=%s RETURNING "
                "session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at",
                (
                    initial_balance,
                    initial_balance,
                    _jsonb({}),
                    now,
                    normalized_id,
                ),
            )
            row = cur.fetchone()
            cur.execute(
                "INSERT INTO replay_events("
                "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                ") VALUES(%s,0,'session.reset',NULL,%s,%s)",
                (
                    normalized_id,
                    _jsonb(
                        {
                            "reason": "operator_reset",
                            "candleSnapshotPreserved": True,
                            "stepRequestHistoryPreserved": True,
                        }
                    ),
                    now,
                ),
            )
        db.commit()

    session = _session_row(row)
    if session is None:
        raise ReplaySessionRepositoryError(
            "Replay session reset did not return persistent state."
        )
    return session
