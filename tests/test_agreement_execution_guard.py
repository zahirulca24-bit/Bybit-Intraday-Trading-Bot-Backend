from __future__ import annotations

import threading
from types import SimpleNamespace

from backend import agreement_execution_guard


class FakeCore:
    def __init__(self):
        self.submitted = []

    def place_demo_order(self, symbol, *args, **kwargs):
        self.submitted.append((symbol, args, kwargs))
        return {"retCode": 0, "retMsg": "OK", "result": {"symbol": symbol}}


def _fake_setup_worker(queue=None):
    state = {
        "confirmedQueue": list(queue or []),
        "rows": [],
        "status": "idle",
    }
    lock = threading.Lock()

    def queue_candidate(candidate, queue_limit):
        queued = list(state.get("confirmedQueue") or [])
        queued.append(dict(candidate))
        state["confirmedQueue"] = queued[-queue_limit:]
        return True

    def snapshot():
        return {
            "status": state.get("status"),
            "confirmedQueue": [dict(row) for row in state.get("confirmedQueue") or []],
            "confirmedQueueSize": len(state.get("confirmedQueue") or []),
        }

    def pop_confirmed():
        queued = list(state.get("confirmedQueue") or [])
        if not queued:
            return None
        first = queued.pop(0)
        state["confirmedQueue"] = queued
        return dict(first)

    return SimpleNamespace(
        _STATE=state,
        _LOCK=lock,
        _queue_candidate=queue_candidate,
        snapshot=snapshot,
        pop_confirmed=pop_confirmed,
    )


def _fake_handoff():
    calls = {"run_once": 0}

    def run_once(core, setup_worker, now=None):
        calls["run_once"] += 1
        core.place_demo_order("BTCUSDT", "Buy", 1, "test", 1, 2)
        return {"currentResult": {"status": "executed"}}

    def snapshot_with_result(result):
        return {"currentResult": dict(result)}

    return SimpleNamespace(run_once=run_once, snapshot_with_result=snapshot_with_result, calls=calls)


def test_restricted_symbol_is_rejected_at_setup_queue_insert():
    core = FakeCore()
    setup_worker = _fake_setup_worker()
    handoff = _fake_handoff()

    agreement_execution_guard.install(core, setup_worker, handoff)

    queued = setup_worker._queue_candidate(
        {"candidateKey": "CLUSDT:1:Buy", "symbol": "CLUSDT", "side": "Buy"},
        10,
    )

    assert queued is False
    assert setup_worker.snapshot()["confirmedQueue"] == []
    assert setup_worker._STATE["lastAgreementExecutionGuard"]["code"] == "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"


def test_restricted_cached_candidate_is_pruned_before_handoff():
    core = FakeCore()
    setup_worker = _fake_setup_worker(
        [
            {"candidateKey": "CLUSDT:1:Buy", "symbol": "CLUSDT", "side": "Buy"},
            {"candidateKey": "BTCUSDT:1:Buy", "symbol": "BTCUSDT", "side": "Buy"},
        ]
    )
    handoff = _fake_handoff()

    agreement_execution_guard.install(core, setup_worker, handoff)
    result = handoff.run_once(core, setup_worker, now=123)

    assert result["currentResult"]["status"] == "blocked"
    assert result["currentResult"]["code"] == "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"
    assert result["currentResult"]["blockedSymbols"] == ["CLUSDT"]
    assert handoff.calls["run_once"] == 0
    assert core.submitted == []
    assert setup_worker.snapshot()["confirmedQueue"] == [
        {"candidateKey": "BTCUSDT:1:Buy", "symbol": "BTCUSDT", "side": "Buy"}
    ]


def test_final_order_submit_guard_blocks_bybit_request_layer():
    core = FakeCore()
    setup_worker = _fake_setup_worker()
    handoff = _fake_handoff()

    agreement_execution_guard.install(core, setup_worker, handoff)
    result = core.place_demo_order("MUUSDT", "Buy", 1, "test", 1, 2)

    assert result["retCode"] == -1
    assert result["agreementExecutionGuard"]["code"] == "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"
    assert result["fillVerification"]["state"] == "Rejected"
    assert core.submitted == []
