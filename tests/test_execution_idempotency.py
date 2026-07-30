from __future__ import annotations

import copy
import types

import pytest

from backend import execution_idempotency


class FakeStore:
    def __init__(self, initial=None):
        self.data = copy.deepcopy(initial or {})
        self.history = []

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def put(self, key, value):
        self.data[key] = copy.deepcopy(value)
        self.history.append(("put", key, copy.deepcopy(value)))

    def put_if_absent(self, key, value):
        if key in self.data:
            return False
        self.data[key] = copy.deepcopy(value)
        self.history.append(("put_if_absent", key, copy.deepcopy(value)))
        return True

    def status(self):
        return {
            "ok": True,
            "persistentPathConfigured": True,
            "degraded": False,
        }


class FakeHandoff:
    _ACTIVE_CLAIM_KEY = "execution_handoff_active_claim"

    def __init__(self):
        self._restart_safe_idempotency_installed = False
        self.create_calls = 0

    def _persist_claim(self, store, claim):
        store.put(self._ACTIVE_CLAIM_KEY, claim)
        return store.get(self._ACTIVE_CLAIM_KEY)

    def _create_claim(self, store, candidate, timestamp):
        self.create_calls += 1
        existing = store.get(self._ACTIVE_CLAIM_KEY)
        if existing is not None:
            return None, existing
        claim = {
            "version": 1,
            "claimId": f"claim-{self.create_calls}",
            "candidateKey": candidate["candidateKey"],
            "candidate": dict(candidate),
            "state": "CLAIMED",
            "claimedAt": timestamp,
            "updatedAt": timestamp,
            "queueRemovalPending": False,
            "requiresOperatorReview": False,
        }
        store.put(self._ACTIVE_CLAIM_KEY, claim)
        return store.get(self._ACTIVE_CLAIM_KEY), None

    def _transition_claim(
        self,
        store,
        claim,
        state,
        timestamp,
        **changes,
    ):
        updated = {
            **dict(claim),
            **changes,
            "state": state,
            "updatedAt": timestamp,
        }
        store.put(self._ACTIVE_CLAIM_KEY, updated)
        return store.get(self._ACTIVE_CLAIM_KEY)


class FakeCore:
    def __init__(self, store=None):
        self._durable_state_store = store or FakeStore()
        self.generated = []
        self.generate_order_link_id = self._random_order_link_id

    def _random_order_link_id(self, source):
        self.generated.append(source)
        return f"random-{source}"


def candidate(key="BTCUSDT:1722100000000:Buy"):
    return {
        "candidateKey": key,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "signalCandleTime": 1722100000000,
    }


def install():
    core = FakeCore()
    handoff = FakeHandoff()
    status = execution_idempotency.install(core, handoff)
    return core, handoff, status


def ledger(store, key):
    return store.get(execution_idempotency._ledger_key(key))


def test_first_candidate_is_reserved_before_active_claim_and_gets_stable_order_id():
    core, handoff, status = install()
    row = candidate()

    claim, conflict = handoff._create_claim(
        core._durable_state_store,
        row,
        1000,
    )

    assert conflict is None
    assert status["installed"] is True
    assert claim["state"] == "CLAIMED"
    assert claim["candidateKey"] == row["candidateKey"]
    assert claim["orderLinkId"] == execution_idempotency._deterministic_order_link_id(
        row["candidateKey"]
    )
    assert len(claim["orderLinkId"]) == 36

    actions = [entry[0] for entry in core._durable_state_store.history]
    assert actions[:2] == ["put_if_absent", "put"]
    assert ledger(core._durable_state_store, row["candidateKey"])["state"] == "CLAIMED"


def test_setup_worker_order_link_id_is_deterministic_across_restart():
    core, handoff, _status = install()
    row = candidate()
    claim, _ = handoff._create_claim(core._durable_state_store, row, 1000)

    first = core.generate_order_link_id("setup-worker")
    second = core.generate_order_link_id("setup-worker")

    assert first == claim["orderLinkId"]
    assert second == claim["orderLinkId"]
    assert first == execution_idempotency._deterministic_order_link_id(
        row["candidateKey"]
    )
    assert core.generated == []

    restarted_core = FakeCore(store=core._durable_state_store)
    restarted_handoff = FakeHandoff()
    execution_idempotency.install(restarted_core, restarted_handoff)

    assert restarted_core.generate_order_link_id("setup-worker") == first


def test_non_setup_order_sources_keep_original_random_generator():
    core, _handoff, _status = install()

    value = core.generate_order_link_id("manual")

    assert value == "random-manual"
    assert core.generated == ["manual"]


def test_completed_candidate_remains_permanently_blocked_after_active_claim_clear():
    core, handoff, _status = install()
    row = candidate()
    claim, _ = handoff._create_claim(core._durable_state_store, row, 1000)

    claim = handoff._transition_claim(
        core._durable_state_store,
        claim,
        "SUBMITTED",
        1001,
        submittedAt=1001,
        orderResponse={
            "retCode": 0,
            "exchangeRetCode": 0,
            "result": {
                "orderId": "exchange-order-1",
                "orderLinkId": claim["orderLinkId"],
            },
        },
    )
    claim = handoff._transition_claim(
        core._durable_state_store,
        claim,
        "RESOLVED_FILLED",
        1002,
        resolvedAt=1002,
        fillDecision={"accepted": True, "code": "ORDER_FILLED"},
    )
    handoff._transition_claim(
        core._durable_state_store,
        claim,
        "COMPLETED",
        1003,
        completedAt=1003,
    )

    record = ledger(core._durable_state_store, row["candidateKey"])
    assert record["state"] == "COMPLETED"
    assert record["terminalOutcome"] == "FILLED"
    assert record["exchangeOrderId"] == "exchange-order-1"

    core._durable_state_store.data.pop(handoff._ACTIVE_CLAIM_KEY)
    restarted_handoff = FakeHandoff()
    restarted_core = FakeCore(store=core._durable_state_store)
    execution_idempotency.install(restarted_core, restarted_handoff)

    duplicate, conflict = restarted_handoff._create_claim(
        restarted_core._durable_state_store,
        row,
        2000,
    )

    assert duplicate is None
    assert conflict["state"] == "IDEMPOTENCY_COMPLETED"
    assert restarted_handoff.create_calls == 0
    assert restarted_core._durable_state_store.get(
        restarted_handoff._ACTIVE_CLAIM_KEY
    ) is None


@pytest.mark.parametrize(
    "state",
    ["SUBMITTED", "UNRESOLVED", "SUBMISSION_UNKNOWN"],
)
def test_unresolved_or_submitted_candidate_is_blocked_after_restart(state):
    key = candidate()["candidateKey"]
    fingerprint = execution_idempotency._fingerprint(key)
    ledger_key = execution_idempotency._ledger_key(key)
    record = {
        "version": 1,
        "candidateKey": key,
        "fingerprint": fingerprint,
        "orderLinkId": execution_idempotency._deterministic_order_link_id(key),
        "state": state,
        "createdAt": 900,
        "updatedAt": 950,
        "candidate": candidate(),
        "claimId": "claim-old",
        "requiresOperatorReview": state != "SUBMITTED",
    }
    store = FakeStore(initial={ledger_key: record})
    core = FakeCore(store=store)
    handoff = FakeHandoff()
    execution_idempotency.install(core, handoff)

    created, conflict = handoff._create_claim(store, candidate(), 1000)

    assert created is None
    assert conflict["state"] == f"IDEMPOTENCY_{state}"
    assert handoff.create_calls == 0


def test_crash_after_reservation_before_active_claim_blocks_future_submission():
    core, handoff, _status = install()
    row = candidate()
    ledger_key, reservation = execution_idempotency._reservation(row, 1000)
    core._durable_state_store.put_if_absent(ledger_key, reservation)

    created, conflict = handoff._create_claim(
        core._durable_state_store,
        row,
        1100,
    )

    assert created is None
    assert conflict["state"] == "IDEMPOTENCY_RESERVED"
    assert handoff.create_calls == 0


def test_legacy_active_claim_gets_bootstrapped_ledger_on_transition():
    core, handoff, _status = install()
    row = candidate()
    legacy_claim = {
        "version": 1,
        "claimId": "legacy-claim",
        "candidateKey": row["candidateKey"],
        "candidate": row,
        "state": "CLAIMED",
        "claimedAt": 900,
        "updatedAt": 900,
    }
    core._durable_state_store.put(handoff._ACTIVE_CLAIM_KEY, legacy_claim)

    updated = handoff._transition_claim(
        core._durable_state_store,
        legacy_claim,
        "SUBMITTED",
        1000,
        submittedAt=1000,
    )

    record = ledger(core._durable_state_store, row["candidateKey"])
    assert updated["orderLinkId"] == record["orderLinkId"]
    assert record["state"] == "SUBMITTED"
    assert record["recoveredLegacyClaim"] is True


def test_setup_worker_order_id_fails_closed_without_active_claim():
    core, _handoff, _status = install()

    with pytest.raises(RuntimeError, match="without a durable active claim"):
        core.generate_order_link_id("setup-worker")


def test_tampered_active_claim_order_id_fails_closed():
    core, handoff, _status = install()
    row = candidate()
    claim, _ = handoff._create_claim(core._durable_state_store, row, 1000)
    claim["orderLinkId"] = "tampered"
    core._durable_state_store.put(handoff._ACTIVE_CLAIM_KEY, claim)

    with pytest.raises(RuntimeError, match="failed identity validation"):
        core.generate_order_link_id("setup-worker")


def test_install_requires_durable_store():
    core = types.SimpleNamespace(
        generate_order_link_id=lambda _source: "random"
    )
    handoff = FakeHandoff()

    with pytest.raises(RuntimeError, match="requires the durable state runtime"):
        execution_idempotency.install(core, handoff)
