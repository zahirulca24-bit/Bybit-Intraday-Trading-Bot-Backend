import inspect
import os
import uuid

import pytest

from backend import execution_command_outbox as outbox
from backend.execution_command_storage import (
    ACTIVE_COMMAND_STATES,
    COMMAND_STATES,
    EXECUTION_COMMAND_MIGRATION,
    ExecutionCommandStorageMixin,
)
from backend.postgres_state_store import MIGRATIONS, PostgresStateStore


class MemoryOutboxStore:
    def __init__(self):
        self.commands = {}

    def status(self):
        return {
            "ok": True,
            "degraded": False,
            "restartSafe": True,
            "backend": "postgresql",
            "migrationVersion": 5,
        }

    def publish_execution_command(self, candidate_key, payload, created_at=None):
        if candidate_key in self.commands:
            return False
        self.commands[candidate_key] = {
            "candidateKey": candidate_key,
            "slotId": None,
            "state": "AVAILABLE",
            "payload": dict(payload),
            "ownerId": None,
            "createdAt": int(created_at or 0),
            "updatedAt": int(created_at or 0),
        }
        return True

    def get_execution_command(self, candidate_key):
        command = self.commands.get(candidate_key)
        return dict(command) if command else None

    def list_execution_commands(self, states=None, limit=100):
        rows = list(self.commands.values())
        if states:
            rows = [row for row in rows if row["state"] in set(states)]
        return [dict(row) for row in rows[:limit]]

    def claim_execution_command(self, owner_id, slot_id, now=None):
        raise AssertionError("Python publisher must not claim commands")

    def transition_execution_command(
        self, candidate_key, owner_id, expected_state, next_state, now=None
    ):
        raise AssertionError("Python publisher must not transition commands")


class CoreStub:
    def __init__(self, store=None, rows=None):
        self._durable_state_store = store or MemoryOutboxStore()
        self._sizing = sizing_snapshot(*(rows or [candidate()]))
        self.order_calls = 0

    def position_sizing_margin_status(self):
        return dict(self._sizing)

    def place_demo_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("Outbox support must not submit orders")


def candidate(key="BTCUSDT:2700000:3600000:Buy:Trend Follow", **updates):
    row = {
        "candidateKey": key,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "strategy": "Trend Follow",
        "grade": "A+",
        "entryReference": 100.0,
        "technicalStopLoss": 99.0,
        "takeProfitReference": 102.0,
        "qty": "10",
        "notional": "1000",
        "requiredInitialMarginUsdt": 100.0,
        "marginMode": "ISOLATED",
        "leverage": 10,
        "positionSizingStatus": "SIZING_APPROVED",
        "sizingApproved": True,
        "executionStatus": "AWAITING_NODE_EXECUTION",
        "nodeExecutionRequirements": {
            "marginMode": "ISOLATED",
            "leverage": 10,
            "maximumLeverage": 10,
            "revalidateWalletAndInstrumentRules": True,
            "submitOnlyAfterRevalidation": True,
        },
        "orderSubmitted": False,
        "tradeRejected": False,
    }
    row.update(updates)
    return row


def sizing_snapshot(*rows):
    return {
        "status": "ready" if rows else "empty",
        "inputFingerprint": "sizing:fingerprint",
        "approvedSizingQueue": list(rows),
        "approvedSizingQueueSize": len(rows),
    }


@pytest.fixture(autouse=True)
def reset_outbox():
    outbox._reset_for_tests()
    yield
    outbox._reset_for_tests()


def test_publisher_inserts_exact_immutable_sizing_payload_without_order_side_effect():
    core = CoreStub()
    outbox.install(core)

    result = outbox.build(core, now=5000)

    assert result["status"] == "ready"
    assert result["metrics"]["published"] == 1
    assert result["metrics"]["blocked"] == 0
    assert result["metrics"]["claimOperations"] == 0
    assert result["orderSubmissions"] == 0
    assert result["tradeRejectionAuthority"] is False
    assert core.order_calls == 0
    stored = core._durable_state_store.get_execution_command(candidate()["candidateKey"])
    assert stored["state"] == "AVAILABLE"
    assert stored["slotId"] is None
    assert stored["ownerId"] is None
    assert stored["payload"] == candidate()


def test_duplicate_publish_is_idempotent_and_never_overwrites_payload():
    store = MemoryOutboxStore()
    original = candidate()
    core = CoreStub(store=store, rows=[original])

    first = outbox.build(core, now=5000)
    second = outbox.build(core, now=5100)

    assert first["metrics"]["published"] == 1
    assert second["metrics"]["idempotentDuplicates"] == 1
    assert second["rows"][0]["code"] == "COMMAND_ALREADY_EXISTS"
    assert second["rows"][0]["tradeRejected"] is False
    assert store.get_execution_command(original["candidateKey"])["payload"] == original


def test_same_candidate_key_with_different_payload_waits_for_reconciliation_without_trade_rejection():
    store = MemoryOutboxStore()
    original = candidate()
    store.publish_execution_command(original["candidateKey"], original, created_at=1)
    changed = candidate(qty="11")
    core = CoreStub(store=store, rows=[changed])

    result = outbox.build(core, now=5000)

    assert result["status"] == "degraded"
    assert result["metrics"]["immutableConflicts"] == 1
    assert result["metrics"]["blocked"] == 0
    assert result["rows"][0]["state"] == "WAIT_RETRY"
    assert result["rows"][0]["code"] == "IMMUTABLE_PAYLOAD_CONFLICT"
    assert result["rows"][0]["tradeRejected"] is False
    assert result["lastError"]
    assert store.get_execution_command(original["candidateKey"])["payload"] == original


def test_invalid_sizing_contract_waits_before_publish_without_trade_rejection():
    store = MemoryOutboxStore()
    core = CoreStub(store=store, rows=[candidate(marginMode="CROSS")])

    result = outbox.build(core, now=5000)

    assert result["status"] == "degraded"
    assert result["metrics"]["blocked"] == 0
    assert result["metrics"]["waitingRetry"] == 1
    assert result["rows"][0]["state"] == "WAIT_RETRY"
    assert result["rows"][0]["code"] == "EXECUTION_PAYLOAD_NOT_READY"
    assert result["rows"][0]["tradeRejected"] is False
    assert store.commands == {}


def test_unavailable_or_non_postgres_store_is_degraded_support_not_trade_rejection():
    class BadStore(MemoryOutboxStore):
        def status(self):
            return {"ok": False, "degraded": True, "backend": "memory"}

    core = CoreStub(store=BadStore())

    result = outbox.build(core, now=5000)

    assert result["status"] == "degraded"
    assert result["metrics"]["blocked"] == 0
    assert result["metrics"]["waitingRetry"] == 1
    assert result["rows"][0]["state"] == "WAIT_RETRY"
    assert result["rows"][0]["tradeRejected"] is False
    assert "PostgreSQL" in result["lastError"]


def test_outbox_accepts_isolated_leverage_up_to_10x():
    core = CoreStub(rows=[candidate(leverage=7, nodeExecutionRequirements={
        "marginMode": "ISOLATED",
        "leverage": 7,
        "maximumLeverage": 10,
        "revalidateWalletAndInstrumentRules": True,
        "submitOnlyAfterRevalidation": True,
    })])

    result = outbox.build(core, now=5000)

    assert result["status"] == "ready"
    assert result["metrics"]["published"] == 1
    assert result["metrics"]["blocked"] == 0


def test_step9_migration_and_claim_contract_are_registered():
    assert EXECUTION_COMMAND_MIGRATION[0] == 5
    assert max(version for version, _ in MIGRATIONS) == 5
    sql = "\n".join(EXECUTION_COMMAND_MIGRATION[1])
    assert "CREATE TABLE IF NOT EXISTS execution_commands" in sql
    assert "PRIMARY KEY" in sql
    assert "ux_execution_commands_active_slot" in sql
    assert "BETWEEN 1 AND 3" in sql
    source = inspect.getsource(ExecutionCommandStorageMixin.claim_execution_command)
    assert "FOR UPDATE SKIP LOCKED" in source
    assert set(COMMAND_STATES) == {
        "AVAILABLE",
        "RESERVED",
        "ORDER_PENDING",
        "PARTIALLY_FILLED",
        "MANAGING",
        "CLOSING",
        "CLOSED",
        "FAILED",
    }


@pytest.fixture
def postgres_store():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL outbox integration")
    store = PostgresStateStore(database_url)
    with store.connect() as db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM execution_commands")
        db.commit()
    yield store
    with store.connect() as db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM execution_commands")
        db.commit()


def _unique_candidate(index):
    key = f"step9-{uuid.uuid4().hex}-{index}"
    return candidate(key=key, symbol=f"T{index}USDT")


def test_postgres_publish_is_immutable_and_candidate_key_idempotent(postgres_store):
    first = _unique_candidate(1)
    changed = dict(first, qty="999")

    assert postgres_store.publish_execution_command(
        first["candidateKey"], first, created_at=100
    ) is True
    assert postgres_store.publish_execution_command(
        first["candidateKey"], changed, created_at=200
    ) is False

    stored = postgres_store.get_execution_command(first["candidateKey"])
    assert stored["state"] == "AVAILABLE"
    assert stored["payload"] == first
    assert stored["createdAt"] == 100


def test_postgres_three_slots_claim_and_owner_guarded_transitions(postgres_store):
    commands = [_unique_candidate(index) for index in range(1, 5)]
    for index, command in enumerate(commands, start=1):
        assert postgres_store.publish_execution_command(
            command["candidateKey"], command, created_at=100 + index
        )

    claimed = [
        postgres_store.claim_execution_command("node-a", slot, now=200 + slot)
        for slot in (1, 2, 3)
    ]
    assert all(row and row["state"] == "RESERVED" for row in claimed)
    assert {row["slotId"] for row in claimed} == {1, 2, 3}
    assert postgres_store.count_active_execution_commands() == 3

    assert postgres_store.claim_execution_command("node-b", 1, now=300) is None
    assert postgres_store.claim_execution_command("node-b", 2, now=300) is None

    first = claimed[0]
    assert postgres_store.transition_execution_command(
        first["candidateKey"],
        "wrong-owner",
        "RESERVED",
        "ORDER_PENDING",
        now=400,
    ) is None
    pending = postgres_store.transition_execution_command(
        first["candidateKey"],
        "node-a",
        "RESERVED",
        "ORDER_PENDING",
        now=401,
    )
    assert pending["state"] == "ORDER_PENDING"
    managing = postgres_store.transition_execution_command(
        first["candidateKey"],
        "node-a",
        "ORDER_PENDING",
        "MANAGING",
        now=402,
    )
    assert managing["state"] == "MANAGING"
    closed = postgres_store.transition_execution_command(
        first["candidateKey"],
        "node-a",
        "MANAGING",
        "CLOSED",
        now=403,
    )
    assert closed["state"] == "CLOSED"
    assert postgres_store.count_active_execution_commands() == 2

    replacement = postgres_store.claim_execution_command("node-b", 1, now=500)
    assert replacement is not None
    assert replacement["slotId"] == 1
    assert replacement["state"] == "RESERVED"


def test_claim_has_no_automatic_expiry_and_invalid_transition_is_rejected(postgres_store):
    command = _unique_candidate(1)
    postgres_store.publish_execution_command(
        command["candidateKey"], command, created_at=100
    )
    claimed = postgres_store.claim_execution_command("node-a", 1, now=200)

    later = postgres_store.get_execution_command(command["candidateKey"])
    assert later["state"] == "RESERVED"
    assert later["ownerId"] == "node-a"
    assert later["slotId"] == 1
    assert set(ACTIVE_COMMAND_STATES).issuperset({later["state"]})

    with pytest.raises(ValueError):
        postgres_store.transition_execution_command(
            command["candidateKey"],
            "node-a",
            "RESERVED",
            "CLOSED",
            now=10000,
        )
