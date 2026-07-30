from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

from backend import execution_handoff_safety_hotfix as hotfix


class Journal:
    def __init__(self, *, fail=False):
        self.entries = []
        self.fail = fail

    def add(self, event, payload):
        if self.fail:
            raise RuntimeError("journal unavailable")
        self.entries.append({"time": 1000, "event": event, "payload": copy.deepcopy(payload)})


class Engine:
    def __init__(self, *, journal_fail=False):
        self.journal = Journal(fail=journal_fail)
        self.status_calls = []

    def set_status(self, *args):
        self.status_calls.append(args)


class Core:
    def __init__(self, *, journal_fail=False):
        self.BOT_LOCK = threading.Lock()
        self.BOT_STATE = {
            "enabled": True,
            "lastTradeAt": 0,
            "lastReason": "startup",
        }
        self.engine = Engine(journal_fail=journal_fail)

    def get_bot_engine(self):
        return self.engine


class Store:
    def __init__(self):
        self.claim = None



def make_handoff(observations):
    def original_fill(order):
        return {
            "accepted": False,
            "code": "ORDER_CREATE_REJECTED",
            "reason": str((order or {}).get("retMsg") or "rejected"),
            "requiresOperatorReview": False,
            "verification": None,
        }

    def original_complete(core, setup_worker, store, claim, timestamp, *, recovery):
        observations["complete_calls"] = observations.get("complete_calls", 0) + 1
        observations["journal_count_before_complete"] = len(core.engine.journal.entries)
        observations["last_trade_at_before_complete"] = core.BOT_STATE["lastTradeAt"]
        observations["claim_before_complete"] = copy.deepcopy(claim)
        return {"completed": True, "recovery": recovery}

    def transition_claim(store, claim, state, timestamp, **changes):
        updated = {
            **copy.deepcopy(claim),
            **copy.deepcopy(changes),
            "state": state,
            "updatedAt": timestamp,
        }
        store.claim = copy.deepcopy(updated)
        return updated

    def disable_execution(core, reason):
        with core.BOT_LOCK:
            core.BOT_STATE["enabled"] = False
            core.BOT_STATE["lastReason"] = reason
            core.BOT_STATE["executionGuard"] = {"ok": False, "reason": reason}

    def result(status, code, reason, candidate=None, **extra):
        return {
            "status": status,
            "code": code,
            "reason": reason,
            "candidateKey": (candidate or {}).get("candidateKey"),
            **extra,
        }

    def record(core, result, timestamp):
        observations["recorded"] = copy.deepcopy(result)
        return {"lastResult": copy.deepcopy(result), "lastRunAt": timestamp}

    def claim_summary(claim):
        return {
            "claimId": claim.get("claimId"),
            "candidateKey": claim.get("candidateKey"),
            "state": claim.get("state"),
        }

    return SimpleNamespace(
        _mandatory_fill_decision=original_fill,
        _complete_resolved_claim=original_complete,
        _transition_claim=transition_claim,
        _disable_execution=disable_execution,
        _result=result,
        _record=record,
        _claim_summary=claim_summary,
    )



def filled_claim():
    return {
        "claimId": "claim-1",
        "candidateKey": "BTCUSDT:1:Buy",
        "candidate": {
            "candidateKey": "BTCUSDT:1:Buy",
            "symbol": "BTCUSDT",
            "side": "Buy",
        },
        "state": "RESOLVED_FILLED",
        "claimedAt": 900,
        "submittedAt": 910,
        "resolvedAt": 920,
        "orderResponse": {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "order-1", "orderLinkId": "link-1"},
        },
        "fillDecision": {
            "accepted": True,
            "code": "ORDER_FILLED",
            "requiresOperatorReview": False,
        },
    }



def test_transport_sentinel_remains_unresolved_and_requires_review():
    observations = {}
    core = Core()
    handoff = make_handoff(observations)
    hotfix.install(core, handoff)

    decision = handoff._mandatory_fill_decision(
        {"retCode": -2, "retMsg": "timed out", "result": {}}
    )

    assert decision["accepted"] is False
    assert decision["code"] == "ORDER_SUBMISSION_TRANSPORT_UNKNOWN"
    assert decision["requiresOperatorReview"] is True
    assert decision["transportUnknown"] is True



def test_definitive_exchange_rejection_still_uses_original_decision():
    observations = {}
    core = Core()
    handoff = make_handoff(observations)
    hotfix.install(core, handoff)

    decision = handoff._mandatory_fill_decision(
        {"retCode": 10001, "retMsg": "parameter error", "result": {}}
    )

    assert decision["code"] == "ORDER_CREATE_REJECTED"
    assert decision["requiresOperatorReview"] is False



def test_recovered_filled_claim_restores_accounting_before_completion():
    observations = {}
    core = Core()
    store = Store()
    handoff = make_handoff(observations)
    hotfix.install(core, handoff)

    result = handoff._complete_resolved_claim(
        core,
        object(),
        store,
        filled_claim(),
        1000,
        recovery=True,
    )

    assert result == {"completed": True, "recovery": True}
    assert observations["complete_calls"] == 1
    assert observations["journal_count_before_complete"] == 1
    assert observations["last_trade_at_before_complete"] == 920
    assert observations["claim_before_complete"]["recoveryAccountingAppliedAt"] == 1000
    assert core.BOT_STATE["lastOrder"]["result"]["orderId"] == "order-1"
    assert core.BOT_STATE["lastSignal"] == "Buy"
    assert core.engine.journal.entries[0]["event"] == "auto_order"
    assert core.engine.journal.entries[0]["payload"]["recoveryClaimId"] == "claim-1"



def test_recovery_deduplicates_existing_claim_journal_event():
    observations = {}
    core = Core()
    core.engine.journal.entries.append(
        {
            "time": 950,
            "event": "auto_order",
            "payload": {"durableClaim": {"claimId": "claim-1"}},
        }
    )
    store = Store()
    handoff = make_handoff(observations)
    hotfix.install(core, handoff)

    handoff._complete_resolved_claim(
        core,
        object(),
        store,
        filled_claim(),
        1000,
        recovery=True,
    )

    assert len(core.engine.journal.entries) == 1
    assert observations["claim_before_complete"]["recoveryAccountingJournalAdded"] is False
    assert core.BOT_STATE["lastTradeAt"] == 920



def test_recovery_accounting_failure_blocks_and_does_not_complete_claim():
    observations = {}
    core = Core(journal_fail=True)
    store = Store()
    handoff = make_handoff(observations)
    hotfix.install(core, handoff)

    result = handoff._complete_resolved_claim(
        core,
        object(),
        store,
        filled_claim(),
        1000,
        recovery=True,
    )

    assert result["lastResult"]["code"] == "RECOVERY_ACCOUNTING_FAILED"
    assert observations.get("complete_calls", 0) == 0
    assert core.BOT_STATE["enabled"] is False
    assert "claim remains active" in core.BOT_STATE["lastReason"]
