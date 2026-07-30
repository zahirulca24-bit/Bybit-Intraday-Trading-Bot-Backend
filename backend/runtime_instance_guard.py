"""PostgreSQL-backed single-leader guard for automatic trading workers.

Cloud Run may start more than one container instance.  The HTTP API can remain
available on every instance, but only the process holding this PostgreSQL
session advisory lock is allowed to run scanners or automatic execution.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.RLock()
_CONNECTION: Any | None = None
_DEFAULT_LOCK_ID = 742_026_073_001
_STATE: dict[str, Any] = {
    "status": "not_initialized",
    "leader": False,
    "lockId": _DEFAULT_LOCK_ID,
    "acquiredAt": 0,
    "releasedAt": 0,
    "lastCheckedAt": 0,
    "lastError": None,
    "reason": "Runtime leadership has not been initialized.",
}


def _lock_id() -> int:
    raw = os.environ.get("RUNTIME_LEADER_LOCK_ID", str(_DEFAULT_LOCK_ID))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_LOCK_ID
    # PostgreSQL advisory locks accept signed 64-bit integers.
    if not -(2**63) <= value < 2**63:
        return _DEFAULT_LOCK_ID
    return value


def _set_core_state(core: Any, *, leader: bool, reason: str) -> None:
    lock = getattr(core, "BOT_LOCK", None)
    state = getattr(core, "BOT_STATE", None)
    if lock is None or not isinstance(state, dict):
        return
    with lock:
        state["runtimeExecutionLeader"] = bool(leader)
        state["runtimeLeadership"] = {
            "leader": bool(leader),
            "status": _STATE.get("status"),
            "reason": reason,
        }
        if not leader:
            state["enabled"] = False
            state["executionGuard"] = {"ok": False, "reason": reason}
            state["lastReason"] = reason


def acquire(core: Any) -> dict[str, Any]:
    """Acquire and retain a PostgreSQL session advisory lock."""
    global _CONNECTION
    with _LOCK:
        if _CONNECTION is not None and _STATE.get("leader"):
            return snapshot_unlocked(check_connection=True)

        lock_id = _lock_id()
        _STATE.update({
            "status": "acquiring",
            "leader": False,
            "lockId": lock_id,
            "lastCheckedAt": int(time.time()),
            "lastError": None,
        })

        store = getattr(core, "_durable_state_store", None)
        connect = getattr(store, "connect", None)
        if not callable(connect):
            reason = "Persistent PostgreSQL store is unavailable; runtime leadership denied."
            _STATE.update({"status": "blocked", "reason": reason})
            _set_core_state(core, leader=False, reason=reason)
            return snapshot_unlocked()

        connection = None
        try:
            connection = connect()
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                row = cursor.fetchone()
            leader = bool(row and row[0])
            if not leader:
                connection.close()
                reason = "Another runtime instance owns the automatic-execution leader lock."
                _STATE.update({
                    "status": "standby",
                    "leader": False,
                    "reason": reason,
                    "lastCheckedAt": int(time.time()),
                })
                _set_core_state(core, leader=False, reason=reason)
                return snapshot_unlocked()

            _CONNECTION = connection
            now = int(time.time())
            reason = "This instance owns the automatic-execution leader lock."
            _STATE.update({
                "status": "leader",
                "leader": True,
                "acquiredAt": now,
                "releasedAt": 0,
                "lastCheckedAt": now,
                "lastError": None,
                "reason": reason,
            })
            _set_core_state(core, leader=True, reason=reason)
            return snapshot_unlocked()
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            reason = f"Runtime leadership check failed: {exc}"
            _STATE.update({
                "status": "error",
                "leader": False,
                "lastError": str(exc),
                "lastCheckedAt": int(time.time()),
                "reason": reason,
            })
            _set_core_state(core, leader=False, reason=reason)
            return snapshot_unlocked()


def snapshot_unlocked(*, check_connection: bool = False) -> dict[str, Any]:
    global _CONNECTION
    if check_connection and _CONNECTION is not None and _STATE.get("leader"):
        try:
            with _CONNECTION.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            _STATE["lastCheckedAt"] = int(time.time())
            _STATE["lastError"] = None
        except Exception as exc:
            try:
                _CONNECTION.close()
            except Exception:
                pass
            _CONNECTION = None
            _STATE.update({
                "status": "lost",
                "leader": False,
                "lastCheckedAt": int(time.time()),
                "lastError": str(exc),
                "reason": "Runtime leader connection was lost; automatic execution is blocked.",
            })
    return dict(_STATE)


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return snapshot_unlocked(check_connection=True)


def is_leader() -> bool:
    return bool(snapshot().get("leader"))


def release(core: Any | None = None, reason: str = "Runtime shutdown completed.") -> dict[str, Any]:
    """Release the advisory lock and close its dedicated database session."""
    global _CONNECTION
    with _LOCK:
        connection = _CONNECTION
        lock_id = int(_STATE.get("lockId") or _DEFAULT_LOCK_ID)
        if connection is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
            except Exception as exc:
                _STATE["lastError"] = str(exc)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
        _CONNECTION = None
        _STATE.update({
            "status": "released",
            "leader": False,
            "releasedAt": int(time.time()),
            "lastCheckedAt": int(time.time()),
            "reason": str(reason),
        })
        if core is not None:
            _set_core_state(core, leader=False, reason=str(reason))
        return snapshot_unlocked()


def install(core: Any) -> dict[str, Any]:
    core.runtime_leadership_status = snapshot
    core.runtime_execution_leader = is_leader
    return acquire(core)
