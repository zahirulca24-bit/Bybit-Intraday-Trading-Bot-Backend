"""PostgreSQL execution-command outbox for the Python-to-Node handoff.

Step 9 publishes immutable Step-8 sizing payloads as AVAILABLE commands.
Node workers claim commands atomically with PostgreSQL row locking. This module
contains no exchange calls, no order submission, and no automatic claim expiry.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

try:
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - surfaced by PostgresStateStore
    Jsonb = None

COMMAND_STATES = (
    "AVAILABLE",
    "RESERVED",
    "ORDER_PENDING",
    "PARTIALLY_FILLED",
    "MANAGING",
    "CLOSING",
    "CLOSED",
    "FAILED",
)
ACTIVE_COMMAND_STATES = (
    "RESERVED",
    "ORDER_PENDING",
    "PARTIALLY_FILLED",
    "MANAGING",
    "CLOSING",
)
TERMINAL_COMMAND_STATES = ("CLOSED", "FAILED")
MAX_ACTIVE_SLOTS = 3

_ALLOWED_TRANSITIONS = {
    "AVAILABLE": {"RESERVED"},
    "RESERVED": {"ORDER_PENDING", "FAILED"},
    "ORDER_PENDING": {"PARTIALLY_FILLED", "MANAGING", "FAILED"},
    "PARTIALLY_FILLED": {"MANAGING", "CLOSING", "FAILED"},
    "MANAGING": {"CLOSING", "CLOSED", "FAILED"},
    "CLOSING": {"CLOSED", "FAILED"},
    "CLOSED": set(),
    "FAILED": set(),
}

_STATE_SQL = ",".join(f"'{state}'" for state in COMMAND_STATES)
_ACTIVE_STATE_SQL = ",".join(f"'{state}'" for state in ACTIVE_COMMAND_STATES)

EXECUTION_COMMAND_MIGRATION = (
    5,
    (
        "CREATE TABLE IF NOT EXISTS execution_commands ("
        "candidate_key TEXT PRIMARY KEY,"
        "slot_id SMALLINT,"
        "state TEXT NOT NULL CHECK(state IN (" + _STATE_SQL + ")),"
        "payload JSONB NOT NULL,"
        "owner_id TEXT,"
        "created_at BIGINT NOT NULL,"
        "updated_at BIGINT NOT NULL,"
        "CHECK(slot_id IS NULL OR slot_id BETWEEN 1 AND 3)"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_execution_commands_state_created "
        "ON execution_commands(state,created_at,candidate_key)",
        "CREATE INDEX IF NOT EXISTS ix_execution_commands_owner_state "
        "ON execution_commands(owner_id,state,updated_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_commands_active_slot "
        "ON execution_commands(slot_id) WHERE slot_id IS NOT NULL AND state IN ("
        + _ACTIVE_STATE_SQL
        + ")",
    ),
)


def _command_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    candidate_key, slot_id, state, payload, owner_id, created_at, updated_at = row
    if not isinstance(payload, dict):
        payload = json.loads(payload)
    return {
        "candidateKey": str(candidate_key),
        "slotId": int(slot_id) if slot_id is not None else None,
        "state": str(state),
        "payload": dict(payload),
        "ownerId": owner_id,
        "createdAt": int(created_at),
        "updatedAt": int(updated_at),
    }


class ExecutionCommandStorageMixin:
    """Atomic execution-command storage mixed into ``PostgresStateStore``."""

    def publish_execution_command(
        self,
        candidate_key: str,
        payload: dict[str, Any],
        *,
        created_at: int | None = None,
    ) -> bool:
        key = str(candidate_key or "").strip()
        if not key:
            raise ValueError("candidate_key is required")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("immutable Step-8 payload is required")
        timestamp = int(created_at or time.time())
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO execution_commands("
                    "candidate_key,slot_id,state,payload,owner_id,created_at,updated_at"
                    ") VALUES(%s,NULL,'AVAILABLE',%s,NULL,%s,%s) "
                    "ON CONFLICT(candidate_key) DO NOTHING RETURNING candidate_key",
                    (key, Jsonb(payload), timestamp, timestamp),
                )
                created = cur.fetchone() is not None
            db.commit()
        return created

    def get_execution_command(self, candidate_key: str) -> dict[str, Any] | None:
        key = str(candidate_key or "").strip()
        if not key:
            return None
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT candidate_key,slot_id,state,payload,owner_id,created_at,updated_at "
                    "FROM execution_commands WHERE candidate_key=%s",
                    (key,),
                )
                row = cur.fetchone()
        return _command_row(row)

    def refresh_stale_execution_command(
        self,
        candidate_key: str,
        payload: dict[str, Any],
        *,
        updated_at: int | None = None,
    ) -> dict[str, Any] | None:
        key = str(candidate_key or "").strip()
        if not key:
            raise ValueError("candidate_key is required")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("fresh Step-8 payload is required")
        timestamp = int(updated_at or time.time())
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE execution_commands SET "
                    "state='AVAILABLE',slot_id=NULL,owner_id=NULL,payload=%s,updated_at=%s "
                    "WHERE candidate_key=%s AND state='AVAILABLE' "
                    "RETURNING candidate_key,slot_id,state,payload,owner_id,created_at,updated_at",
                    (Jsonb(payload), timestamp, key),
                )
                updated = cur.fetchone()
            db.commit()
        return _command_row(updated)

    def list_execution_commands(
        self,
        *,
        states: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized = [
            str(state or "").upper()
            for state in states or []
            if str(state or "").upper() in COMMAND_STATES
        ]
        bounded_limit = max(1, min(1000, int(limit)))
        query = (
            "SELECT candidate_key,slot_id,state,payload,owner_id,created_at,updated_at "
            "FROM execution_commands"
        )
        params: list[Any] = []
        if normalized:
            placeholders = ",".join(["%s"] * len(normalized))
            query += f" WHERE state IN ({placeholders})"
            params.extend(normalized)
        query += " ORDER BY created_at,candidate_key LIMIT %s"
        params.append(bounded_limit)
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
        return [item for item in (_command_row(row) for row in rows) if item]

    def count_active_execution_commands(self) -> int:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM execution_commands WHERE state IN ("
                    + _ACTIVE_STATE_SQL
                    + ")"
                )
                row = cur.fetchone()
        return int((row or [0])[0])

    def claim_execution_command(
        self,
        owner_id: str,
        slot_id: int,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        owner = str(owner_id or "").strip()
        slot = int(slot_id)
        if not owner:
            raise ValueError("owner_id is required")
        if slot not in {1, 2, 3}:
            raise ValueError("slot_id must be 1, 2, or 3")
        timestamp = int(now or time.time())

        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM execution_commands "
                    "WHERE slot_id=%s AND state IN (" + _ACTIVE_STATE_SQL + ") LIMIT 1",
                    (slot,),
                )
                if cur.fetchone() is not None:
                    db.commit()
                    return None

                cur.execute(
                    "SELECT COUNT(*) FROM execution_commands WHERE state IN ("
                    + _ACTIVE_STATE_SQL
                    + ")"
                )
                active = int(cur.fetchone()[0])
                if active >= MAX_ACTIVE_SLOTS:
                    db.commit()
                    return None

                cur.execute(
                    "SELECT candidate_key FROM execution_commands "
                    "WHERE state='AVAILABLE' "
                    "ORDER BY created_at,candidate_key "
                    "FOR UPDATE SKIP LOCKED LIMIT 1"
                )
                selected = cur.fetchone()
                if selected is None:
                    db.commit()
                    return None

                candidate_key = str(selected[0])
                cur.execute(
                    "UPDATE execution_commands SET "
                    "state='RESERVED',slot_id=%s,owner_id=%s,updated_at=%s "
                    "WHERE candidate_key=%s AND state='AVAILABLE' "
                    "RETURNING candidate_key,slot_id,state,payload,owner_id,created_at,updated_at",
                    (slot, owner, timestamp, candidate_key),
                )
                claimed = cur.fetchone()
            db.commit()
        return _command_row(claimed)

    def transition_execution_command(
        self,
        candidate_key: str,
        owner_id: str,
        expected_state: str,
        next_state: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        key = str(candidate_key or "").strip()
        owner = str(owner_id or "").strip()
        expected = str(expected_state or "").upper()
        target = str(next_state or "").upper()
        if not key or not owner:
            raise ValueError("candidate_key and owner_id are required")
        if target not in _ALLOWED_TRANSITIONS.get(expected, set()):
            raise ValueError(f"Invalid execution command transition: {expected}->{target}")
        timestamp = int(now or time.time())
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE execution_commands SET state=%s,updated_at=%s "
                    "WHERE candidate_key=%s AND owner_id=%s AND state=%s "
                    "RETURNING candidate_key,slot_id,state,payload,owner_id,created_at,updated_at",
                    (target, timestamp, key, owner, expected),
                )
                updated = cur.fetchone()
            db.commit()
        return _command_row(updated)
