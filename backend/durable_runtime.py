"""Install persistent journal and restart recovery into the canonical runtime.

Automatic execution is fail-closed unless a healthy PostgreSQL DATABASE_URL is
available. No local SQLite, memory, or LocalStorage fallback is accepted as a
durable execution claim store.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from . import startup_reconciliation
    from .postgres_state_store import MIGRATIONS, PostgresStateStore
except ImportError:
    import startup_reconciliation
    from postgres_state_store import MIGRATIONS, PostgresStateStore

RISK_KEYS = {
    "tradingDateKey", "lastTradeAt", "lastSignal", "lastReason", "lastOrder",
    "executionGuard", "orderLifecycle", "positionSizing", "tradeManagement",
    "dailyRisk", "maxOpenPositions", "maxTradesPerDay", "dailyLossCapUsdt",
}
_ACTIVE_CLAIM_STATES = {"CLAIMED", "SUBMITTED", "UNRESOLVED", "SUBMISSION_UNKNOWN"}


class UnavailablePersistentStore:
    """Non-persistent sentinel that keeps diagnostics alive but blocks execution."""

    def __init__(self, error: str):
        self.error = str(error)
        self.path = "unavailable:postgresql"

    def status(self):
        return {
            "ok": False,
            "backend": "postgresql",
            "persistentPathConfigured": False,
            "degraded": True,
            "restartSafe": False,
            "automaticExecutionAllowed": False,
            "error": self.error,
        }

    def recent(self, limit=1000):
        return []

    def get(self, key, default=None):
        return default

    def put(self, key, value):
        return None

    def put_if_absent(self, key, value):
        return False

    def compare_and_swap(self, key, expected, replacement):
        return False

    def delete(self, key):
        return None

    def append(self, event, payload=None, ts=None):
        raise RuntimeError(self.error)


def _build_store():
    try:
        store = PostgresStateStore()
        status = store.status()
        if not status.get("ok") or status.get("degraded"):
            raise RuntimeError(status.get("error") or "Persistent PostgreSQL state is unavailable.")
        return store
    except Exception as exc:
        return UnavailablePersistentStore(str(exc))


def execution_readiness(core: Any) -> dict[str, Any]:
    """Return the single authoritative gate used by Start Auto and every handoff."""
    store = getattr(core, "_durable_state_store", None)
    if store is None or not callable(getattr(store, "status", None)):
        return {"ready": False, "reason": "Persistent PostgreSQL state store is not installed."}

    try:
        store_status = dict(store.status() or {})
    except Exception as exc:
        return {"ready": False, "reason": f"Persistent PostgreSQL health check failed: {exc}"}

    required_migration = max((version for version, _ in MIGRATIONS), default=0)
    migration_version = int(store_status.get("migrationVersion") or 0)
    reconciliation = startup_reconciliation.snapshot()
    reconciliation_status = str(reconciliation.get("status") or "not_run")

    try:
        active_claim = store.get("execution_handoff_active_claim")
    except Exception as exc:
        return {
            "ready": False,
            "reason": f"Persistent execution claim could not be read: {exc}",
            "store": store_status,
            "startupReconciliation": reconciliation,
        }

    unresolved_claim = bool(
        isinstance(active_claim, dict)
        and str(active_claim.get("state") or "") in _ACTIVE_CLAIM_STATES
    )

    if not store_status.get("ok") or store_status.get("degraded"):
        reason = "Persistent PostgreSQL state is unavailable or degraded."
    elif not store_status.get("restartSafe"):
        reason = "Persistent state is not restart-safe."
    elif migration_version < required_migration:
        reason = f"Database migration is incomplete ({migration_version}/{required_migration})."
    elif reconciliation_status != "ready":
        reason = f"Startup reconciliation is not ready ({reconciliation_status})."
    elif unresolved_claim:
        reason = "An unresolved execution claim requires operator review."
    elif not bool(core.BOT_STATE.get("persistentStateReady")):
        reason = "Persistent runtime readiness has not been established."
    else:
        reason = "Persistent execution readiness approved."

    ready = reason == "Persistent execution readiness approved."
    return {
        "ready": ready,
        "reason": reason,
        "store": store_status,
        "requiredMigrationVersion": required_migration,
        "startupReconciliation": reconciliation,
        "unresolvedClaim": active_claim if unresolved_claim else None,
    }


def _install_authoritative_execution_gates(core: Any) -> None:
    """Gate Start Auto and direct handoff calls with the same readiness decision."""
    try:
        from . import execution_handoff, guarded_server
    except ImportError:
        import execution_handoff
        import guarded_server

    core.execution_readiness = lambda: execution_readiness(core)

    if not getattr(execution_handoff, "_persistent_readiness_gate_installed", False):
        original_claim_store = execution_handoff._claim_store

        def gated_claim_store(runtime_core: Any):
            readiness = execution_readiness(runtime_core)
            if not readiness.get("ready"):
                status = dict(readiness.get("store") or {})
                status.update({
                    "automaticExecutionAllowed": False,
                    "executionReadiness": readiness,
                })
                return None, status, readiness.get("reason")
            return original_claim_store(runtime_core)

        execution_handoff._claim_store = gated_claim_store
        execution_handoff._persistent_readiness_gate_installed = True

    handler = guarded_server.GuardedHandler
    if not getattr(handler, "_persistent_readiness_gate_installed", False):
        original_start_bot = handler._start_bot

        def gated_start_bot(instance: Any, payload: dict[str, Any]):
            readiness = execution_readiness(core)
            if not readiness.get("ready"):
                with core.BOT_LOCK:
                    core.BOT_STATE.update({
                        "enabled": False,
                        "persistentStateReady": False,
                        "executionGuard": {"ok": False, "reason": readiness["reason"]},
                        "lastReason": readiness["reason"],
                    })
                core.json_response(instance, 503, {
                    "ok": False,
                    "enabled": False,
                    "reason": readiness["reason"],
                    "executionReadiness": readiness,
                })
                return
            return original_start_bot(instance, payload)

        handler._start_bot = gated_start_bot
        handler._persistent_readiness_gate_installed = True


def _record_order_and_fill(store: Any, event: str, payload: dict[str, Any]) -> None:
    if not isinstance(store, PostgresStateStore):
        return
    if event not in {"auto_order", "manual_connection_test", "setup_execution_handoff"}:
        return
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    verification = result.get("fillVerification") if isinstance(result, dict) else None
    order_key = str(
        (result.get("orderId") if isinstance(result, dict) else None)
        or ((result.get("result") or {}).get("orderId") if isinstance(result, dict) else None)
        or payload.get("candidateKey")
        or payload.get("claimId")
        or ""
    )
    if order_key:
        store.record_order(order_key, {**payload, "event": event})
    if isinstance(verification, dict) and (
        verification.get("finalFilled") is True
        or verification.get("state") == "FILLED"
        or verification.get("state") == "Filled"
    ):
        fill_key = str(verification.get("execId") or verification.get("orderId") or order_key)
        if fill_key:
            store.record_fill(fill_key, order_key or None, {**verification, "symbol": payload.get("symbol")})


def install(core: Any):
    existing_store = getattr(core, "_durable_state_store", None)
    if existing_store:
        _install_authoritative_execution_gates(core)
        return existing_store

    store = _build_store()
    status = store.status()
    core._durable_state_store = store
    core.execution_readiness = lambda: execution_readiness(core)

    if not status.get("ok"):
        reconciliation = startup_reconciliation.reconcile(core, store)
        core.durable_state_status = lambda: {
            **store.status(),
            "automaticExecutionAllowed": False,
            "startupReconciliation": reconciliation,
            "executionReadiness": execution_readiness(core),
        }
        with core.BOT_LOCK:
            core.BOT_STATE.update({
                "enabled": False,
                "persistentStateReady": False,
                "executionGuard": {
                    "ok": False,
                    "reason": "Persistent PostgreSQL state unavailable; automatic execution is blocked.",
                },
                "lastReason": "Persistent PostgreSQL state unavailable; automatic execution is blocked.",
            })
        _install_authoritative_execution_gates(core)
        return store

    engine = core.get_bot_engine()
    journal = engine.journal
    persisted = store.recent(getattr(journal, "limit", 1000))
    existing = list(getattr(journal, "entries", []) or [])
    if not persisted and existing:
        for entry in existing:
            store.append(entry.get("event", "legacy"), entry.get("payload") or {}, entry.get("time"))
        persisted = store.recent(getattr(journal, "limit", 1000))
    journal.entries = persisted

    saved_risk = store.get("risk_state", {})
    if isinstance(saved_risk, dict):
        for key in RISK_KEYS:
            if key in saved_risk:
                core.BOT_STATE[key] = saved_risk[key]

    last_risk_fingerprint: str | None = None

    def save_risk():
        nonlocal last_risk_fingerprint
        snapshot = {key: core.BOT_STATE.get(key) for key in RISK_KEYS}
        store.put("risk_state", snapshot)
        fingerprint = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        if fingerprint != last_risk_fingerprint:
            store.record_risk_snapshot(core.BOT_STATE.get("tradingDateKey"), snapshot)
            last_risk_fingerprint = fingerprint

    def durable_add(event, payload=None):
        body = payload or {}
        entry = store.append(event, body)
        _record_order_and_fill(store, event, body)
        with journal._lock:
            journal.entries.append(entry)
            journal.entries = journal.entries[-journal.limit:]
        save_risk()
        return entry

    journal.add = durable_add
    journal.path = "postgresql://runtime/journal"

    try:
        from .engines import order_fill
    except ImportError:
        from engines import order_fill

    saved_pending = store.get("pending_entry")
    if isinstance(saved_pending, dict):
        with order_fill._PENDING_LOCK:
            order_fill._PENDING_ENTRY = saved_pending

    original_register = order_fill._register_pending
    original_clear = order_fill.clear_pending_entry
    original_clear_matching = order_fill._clear_if_matching

    def register_pending(symbol, order_result, verification):
        original_register(symbol, order_result, verification)
        store.put("pending_entry", order_fill.get_pending_entry())

    def clear_pending():
        original_clear()
        store.delete("pending_entry")

    def clear_matching(order_result):
        original_clear_matching(order_result)
        if not order_fill.get_pending_entry():
            store.delete("pending_entry")

    order_fill._register_pending = register_pending
    order_fill.clear_pending_entry = clear_pending
    order_fill._clear_if_matching = clear_matching

    original_tick = core.bot_tick

    def durable_tick():
        result = original_tick()
        save_risk()
        return result

    core.bot_tick = durable_tick
    reconciliation = startup_reconciliation.reconcile(core, store)
    with core.BOT_LOCK:
        core.BOT_STATE["persistentStateReady"] = reconciliation.get("status") == "ready"
        if reconciliation.get("status") != "ready":
            core.BOT_STATE["enabled"] = False

    _install_authoritative_execution_gates(core)
    core.durable_state_status = lambda: {
        **store.status(),
        "automaticExecutionAllowed": execution_readiness(core).get("ready", False),
        "startupReconciliation": startup_reconciliation.snapshot(),
        "executionReadiness": execution_readiness(core),
    }
    save_risk()
    return store
