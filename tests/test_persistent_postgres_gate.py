from __future__ import annotations

import threading
from types import SimpleNamespace

from backend import durable_runtime, execution_handoff, guarded_server, startup_reconciliation
from backend.postgres_state_store import MIGRATIONS


class FakeJournal:
    def __init__(self):
        self.entries = []
        self.limit = 100
        self._lock = threading.RLock()
        self.path = None


class FakeEngine:
    def __init__(self):
        self.journal = FakeJournal()


class FakeStore:
    def __init__(self, status=None, values=None):
        self._status = status or {
            "ok": True,
            "backend": "postgresql",
            "degraded": False,
            "restartSafe": True,
            "persistentPathConfigured": True,
            "migrationVersion": max(version for version, _ in MIGRATIONS),
        }
        self.values = values or {}

    def status(self):
        return dict(self._status)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def put(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def put_if_absent(self, key, value):
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def compare_and_swap(self, key, expected, replacement):
        if self.values.get(key) != expected:
            return False
        self.values[key] = replacement
        return True


class FakeCore:
    def __init__(self):
        self.BOT_LOCK = threading.RLock()
        self.BOT_STATE = {
            "enabled": True,
            "persistentStateReady": False,
            "lastReason": "running",
            "executionGuard": {"ok": True},
        }
        self._engine = FakeEngine()
        self.responses = []

    def get_bot_engine(self):
        return self._engine

    def bot_tick(self):
        return dict(self.BOT_STATE)

    def bybit_request(self, method, path, params):
        raise AssertionError("exchange reconciliation must not run without persistent storage")

    def json_response(self, handler, status, payload):
        self.responses.append((status, payload))


def test_missing_database_fails_closed(monkeypatch):
    monkeypatch.setattr(
        durable_runtime,
        "PostgresStateStore",
        lambda: (_ for _ in ()).throw(RuntimeError("DATABASE_URL is required")),
    )
    monkeypatch.setattr(durable_runtime, "_install_authoritative_execution_gates", lambda core: None)
    core = FakeCore()

    store = durable_runtime.install(core)
    status = core.durable_state_status()

    assert store.status()["ok"] is False
    assert status["degraded"] is True
    assert status["automaticExecutionAllowed"] is False
    assert core.BOT_STATE["enabled"] is False
    assert core.BOT_STATE["persistentStateReady"] is False
    assert core.BOT_STATE["executionGuard"]["ok"] is False


def test_migrations_cover_required_restart_state():
    sql = "\n".join(statement for _, statements in MIGRATIONS for statement in statements)
    for table in (
        "schema_migrations",
        "journal",
        "runtime_state",
        "orders",
        "fills",
        "risk_snapshots",
        "reconciliation_runs",
    ):
        assert table in sql


def test_failed_reconciliation_blocks_authoritative_readiness(monkeypatch):
    core = FakeCore()
    core._durable_state_store = FakeStore()
    core.BOT_STATE["persistentStateReady"] = False
    monkeypatch.setattr(
        startup_reconciliation,
        "snapshot",
        lambda: {"status": "error", "error": "Bybit unavailable"},
    )

    readiness = durable_runtime.execution_readiness(core)

    assert readiness["ready"] is False
    assert "reconciliation" in readiness["reason"].lower()


def test_unresolved_claim_blocks_authoritative_readiness(monkeypatch):
    core = FakeCore()
    core._durable_state_store = FakeStore(values={
        "execution_handoff_active_claim": {"state": "UNRESOLVED", "claimId": "claim-1"}
    })
    core.BOT_STATE["persistentStateReady"] = True
    monkeypatch.setattr(startup_reconciliation, "snapshot", lambda: {"status": "ready"})

    readiness = durable_runtime.execution_readiness(core)

    assert readiness["ready"] is False
    assert "operator review" in readiness["reason"].lower()


def test_direct_handoff_is_blocked_when_reconciliation_not_ready(monkeypatch):
    core = FakeCore()
    core._durable_state_store = FakeStore()
    core.BOT_STATE["persistentStateReady"] = False
    monkeypatch.setattr(startup_reconciliation, "snapshot", lambda: {"status": "error"})
    monkeypatch.setattr(execution_handoff, "_persistent_readiness_gate_installed", False, raising=False)
    monkeypatch.setattr(guarded_server.GuardedHandler, "_persistent_readiness_gate_installed", True, raising=False)

    durable_runtime._install_authoritative_execution_gates(core)
    store, status, error = execution_handoff._claim_store(core)

    assert store is None
    assert status["automaticExecutionAllowed"] is False
    assert "reconciliation" in error.lower()


def test_start_auto_cannot_bypass_failed_reconciliation(monkeypatch):
    core = FakeCore()
    core._durable_state_store = FakeStore()
    core.BOT_STATE["persistentStateReady"] = False
    monkeypatch.setattr(startup_reconciliation, "snapshot", lambda: {"status": "error"})
    monkeypatch.setattr(execution_handoff, "_persistent_readiness_gate_installed", True, raising=False)
    monkeypatch.setattr(guarded_server.GuardedHandler, "_persistent_readiness_gate_installed", False, raising=False)

    original_calls = []
    monkeypatch.setattr(
        guarded_server.GuardedHandler,
        "_start_bot",
        lambda instance, payload: original_calls.append(payload),
    )

    durable_runtime._install_authoritative_execution_gates(core)
    guarded_server.GuardedHandler._start_bot(SimpleNamespace(), {"mode": "balanced"})

    assert original_calls == []
    assert core.BOT_STATE["enabled"] is False
    assert core.responses[0][0] == 503
    assert core.responses[0][1]["enabled"] is False


def test_order_and_fill_payloads_are_persisted(monkeypatch):
    class FakeLedgerStore:
        def __init__(self):
            self.orders = []
            self.fills = []

        def record_order(self, key, payload):
            self.orders.append((key, payload))

        def record_fill(self, key, order_key, payload):
            self.fills.append((key, order_key, payload))

    monkeypatch.setattr(durable_runtime, "PostgresStateStore", FakeLedgerStore)
    store = FakeLedgerStore()
    payload = {
        "symbol": "BTCUSDT",
        "signal": "Buy",
        "result": {
            "result": {"orderId": "order-1"},
            "fillVerification": {
                "finalFilled": True,
                "orderId": "order-1",
                "cumExecQty": "0.001",
                "avgPrice": "50000",
            },
        },
    }

    durable_runtime._record_order_and_fill(store, "auto_order", payload)

    assert store.orders[0][0] == "order-1"
    assert store.fills[0][0] == "order-1"
    assert store.fills[0][1] == "order-1"
