from __future__ import annotations

import copy

import pytest

from backend import execution_idempotency
from backend import execution_idempotency_race_fix


@pytest.fixture(autouse=True)
def restore_process_wide_wrapper():
    original = execution_idempotency._ensure_claim_metadata
    yield
    execution_idempotency._ensure_claim_metadata = original
    execution_idempotency_race_fix._ORIGINAL_ENSURE = None


class FakeStore:
    def __init__(self, initial=None):
        self.data = copy.deepcopy(initial or {})
        self.before_swap = None

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
        if self.before_swap:
            callback = self.before_swap
            self.before_swap = None
            callback()
        if self.data.get(key) != expected:
            return False
        self.data[key] = copy.deepcopy(replacement)
        return True


class Handoff:
    _ACTIVE_CLAIM_KEY = "execution_handoff_active_claim"

    def __init__(self):
        self._idempotency_race_fix_installed = False


class Core:
    def __init__(self, store):
        self._durable_state_store = store


def candidate(key, symbol):
    return {
        "candidateKey": key,
        "symbol": symbol,
        "side": "Buy",
        "signalCandleTime": 1,
    }


def legacy_claim(key="BTCUSDT:1:Buy", claim_id="legacy-a"):
    row = candidate(key, key.split(":", 1)[0])
    return {
        "version": 1,
        "claimId": claim_id,
        "candidateKey": key,
        "candidate": row,
        "state": "UNRESOLVED",
        "claimedAt": 10,
        "updatedAt": 11,
        "requiresOperatorReview": True,
    }


def test_stale_bootstrap_cannot_overwrite_new_active_claim():
    old = legacy_claim()
    store = FakeStore({Handoff._ACTIVE_CLAIM_KEY: old})
    handoff = Handoff()
    core = Core(store)
    execution_idempotency_race_fix.install(core, handoff)

    new = legacy_claim("ETHUSDT:2:Sell", "claim-b")
    new["state"] = "CLAIMED"

    store.before_swap = lambda: store.put(Handoff._ACTIVE_CLAIM_KEY, new)

    with pytest.raises(RuntimeError, match="ownership changed during"):
        execution_idempotency._ensure_claim_metadata(handoff, store, old, 20)

    assert store.get(Handoff._ACTIVE_CLAIM_KEY) == new


def test_same_owner_is_enriched_with_atomic_compare_and_swap():
    old = legacy_claim()
    store = FakeStore({Handoff._ACTIVE_CLAIM_KEY: old})
    handoff = Handoff()
    core = Core(store)
    execution_idempotency_race_fix.install(core, handoff)

    enriched, ledger_key, record = execution_idempotency._ensure_claim_metadata(
        handoff, store, old, 20
    )

    assert enriched["claimId"] == old["claimId"]
    assert enriched["idempotencyLedgerKey"] == ledger_key
    assert enriched["orderLinkId"] == record["orderLinkId"]
    assert store.get(Handoff._ACTIVE_CLAIM_KEY) == enriched


def test_same_owner_concurrent_fields_are_preserved():
    stale = legacy_claim()
    current = {
        **stale,
        "queueRemovalPending": True,
        "recoveryAccounting": {"verified": True},
        "operatorReviewError": "exchange reconciliation pending",
    }
    store = FakeStore({Handoff._ACTIVE_CLAIM_KEY: current})
    handoff = Handoff()
    core = Core(store)
    execution_idempotency_race_fix.install(core, handoff)

    enriched, _ledger_key, _record = execution_idempotency._ensure_claim_metadata(
        handoff, store, stale, 20
    )

    assert enriched["queueRemovalPending"] is True
    assert enriched["recoveryAccounting"] == {"verified": True}
    assert enriched["operatorReviewError"] == "exchange reconciliation pending"
