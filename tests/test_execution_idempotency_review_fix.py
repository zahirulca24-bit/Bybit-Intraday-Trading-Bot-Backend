from __future__ import annotations

import copy

import pytest

from backend import execution_idempotency
from backend import execution_idempotency_race_fix
from backend import execution_idempotency_review_fix


class FakeStore:
    def __init__(self, initial=None):
        self.data = copy.deepcopy(initial or {})

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def put(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def put_if_absent(self, key, value):
        if key in self.data:
            return False
        self.data[key] = copy.deepcopy(value)
        return True

    def compare_and_swap(self, key, expected, replacement):
        if self.data.get(key) != expected:
            return False
        self.data[key] = copy.deepcopy(replacement)
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
        self._idempotency_race_fix_installed = False
        self._idempotency_review_fix_installed = False
        self.counter = 0

    def _persist_claim(self, store, claim):
        store.put(self._ACTIVE_CLAIM_KEY, claim)
        return store.get(self._ACTIVE_CLAIM_KEY)

    def _create_claim(self, store, candidate, timestamp):
        self.counter += 1
        existing = store.get(self._ACTIVE_CLAIM_KEY)
        if existing is not None:
            return None, existing
        claim = {
            "version": 1,
            "claimId": f"claim-{self.counter}",
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

    def _transition_claim(self, store, claim, state, timestamp, **changes):
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


def install(store=None):
    core = FakeCore(store=store)
    handoff = FakeHandoff()
    execution_idempotency.install(core, handoff)
    execution_idempotency_race_fix.install(core, handoff)
    status = execution_idempotency_review_fix.install(core, handoff)
    return core, handoff, status


def create_claim(core, handoff, row=None, timestamp=1000):
    claim, conflict = handoff._create_claim(
        core._durable_state_store,
        row or candidate(),
        timestamp,
    )
    assert conflict is None
    assert claim is not None
    return claim


def test_order_link_id_is_bound_to_exact_submitting_claim_and_consumed_once():
    core, handoff, status = install()
    claim = create_claim(core, handoff)

    value = core.generate_order_link_id("setup-worker")

    assert status["claimBoundOrderLinkId"] is True
    assert value == claim["orderLinkId"]
    assert value == execution_idempotency._deterministic_order_link_id(
        claim["candidateKey"]
    )
    with pytest.raises(RuntimeError, match="without a bound submitting claim"):
        core.generate_order_link_id("setup-worker")


def test_active_claim_replacement_cannot_supply_another_candidates_order_id():
    core, handoff, _status = install()
    claim_a = create_claim(core, handoff, candidate("BTCUSDT:1:Buy"))

    row_b = candidate("ETHUSDT:2:Sell")
    key_b, reservation_b = execution_idempotency._reservation(row_b, 1001)
    reservation_b.update(
        {
            "state": "CLAIMED",
            "claimId": "claim-b",
            "claimedAt": 1001,
        }
    )
    core._durable_state_store.put(key_b, reservation_b)
    active_b = {
        "version": 1,
        "claimId": "claim-b",
        "candidateKey": row_b["candidateKey"],
        "candidate": row_b,
        "candidateFingerprint": reservation_b["fingerprint"],
        "idempotencyLedgerKey": key_b,
        "orderLinkId": reservation_b["orderLinkId"],
        "state": "CLAIMED",
        "claimedAt": 1001,
        "updatedAt": 1001,
    }
    core._durable_state_store.put(handoff._ACTIVE_CLAIM_KEY, active_b)

    with pytest.raises(RuntimeError, match="no longer owns"):
        core.generate_order_link_id("setup-worker")

    assert claim_a["orderLinkId"] != active_b["orderLinkId"]


def test_missing_permanent_ledger_fails_before_order_id_is_returned():
    core, handoff, _status = install()
    claim = create_claim(core, handoff)
    core._durable_state_store.data.pop(claim["idempotencyLedgerKey"])

    with pytest.raises(RuntimeError, match="ledger is missing"):
        core.generate_order_link_id("setup-worker")


def test_corrupt_permanent_ledger_identity_fails_before_submission():
    core, handoff, _status = install()
    claim = create_claim(core, handoff)
    record = core._durable_state_store.get(claim["idempotencyLedgerKey"])
    record["orderLinkId"] = "corrupt"
    core._durable_state_store.put(claim["idempotencyLedgerKey"], record)

    with pytest.raises(RuntimeError, match="orderLinkId mismatch"):
        core.generate_order_link_id("setup-worker")


def test_unresolved_legacy_active_claim_is_bootstrapped_during_install():
    row = candidate("SOLUSDT:3:Buy")
    legacy = {
        "version": 1,
        "claimId": "legacy-unresolved",
        "candidateKey": row["candidateKey"],
        "candidate": row,
        "state": "UNRESOLVED",
        "claimedAt": 900,
        "updatedAt": 950,
        "requiresOperatorReview": True,
        "queueRemovalPending": False,
    }
    store = FakeStore(initial={FakeHandoff._ACTIVE_CLAIM_KEY: legacy})

    core, handoff, status = install(store=store)

    active = store.get(handoff._ACTIVE_CLAIM_KEY)
    ledger = store.get(execution_idempotency._ledger_key(row["candidateKey"]))
    assert status["legacyActiveClaimBootstrap"] is True
    assert active["orderLinkId"] == ledger["orderLinkId"]
    assert ledger["claimId"] == "legacy-unresolved"
    assert ledger["state"] == "UNRESOLVED"
    assert ledger["recoveredLegacyClaim"] is True


def test_transition_without_order_construction_clears_stale_thread_binding():
    core, handoff, _status = install()
    claim = create_claim(core, handoff)

    handoff._transition_claim(
        core._durable_state_store,
        claim,
        "SUBMITTED",
        1001,
        submittedAt=1001,
    )

    with pytest.raises(RuntimeError, match="without a bound submitting claim"):
        core.generate_order_link_id("setup-worker")


def test_non_setup_order_sources_keep_original_generator():
    core, _handoff, _status = install()

    assert core.generate_order_link_id("manual") == "random-manual"
    assert core.generated == ["manual"]
