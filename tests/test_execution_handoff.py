from __future__ import annotations

import copy
import threading

import pytest

from backend import execution_handoff


class Journal:
    def __init__(self):
        self.entries = []

    def add(self, event, payload):
        self.entries.append({"event": event, "payload": payload})


class Engine:
    def __init__(self):
        self.journal = Journal()

    def set_status(self, *_args):
        pass


class FakeStore:
    def __init__(self, *, persistent=True, initial=None, conflict=False):
        self.data = copy.deepcopy(initial or {})
        self.history = []
        self.persistent = persistent
        self.conflict = conflict

    def status(self):
        return {
            "ok": True,
            "path": "/persistent/bot_state.sqlite3",
            "persistentPathConfigured": self.persistent,
            "degraded": not self.persistent,
        }

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def put(self, key, value):
        self.data[key] = copy.deepcopy(value)
        self.history.append(("put", key, copy.deepcopy(value)))

    def put_if_absent(self, key, value):
        if self.conflict or key in self.data:
            return False
        self.data[key] = copy.deepcopy(value)
        self.history.append(("put_if_absent", key, copy.deepcopy(value)))
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.history.append(("delete", key, None))


class SetupWorker:
    def __init__(self, candidate_rows):
        if isinstance(candidate_rows, dict):
            candidate_rows = [candidate_rows]
        self.queue = [dict(row) for row in candidate_rows]
        self.removal_observations = []

    def snapshot(self):
        return {"confirmedQueue": [dict(row) for row in self.queue]}

    def remove_confirmed(self, candidate_key):
        self.removal_observations.append(
            {
                "candidateKey": candidate_key,
                "queueBefore": [dict(row) for row in self.queue],
            }
        )
        for index, row in enumerate(self.queue):
            if row.get("candidateKey") == candidate_key:
                return self.queue.pop(index)
        return None


def verified_fill_order():
    return {
        "retCode": 0,
        "retMsg": "OK: order fully filled",
        "exchangeRetCode": 0,
        "exchangeRetMsg": "OK",
        "accepted": True,
        "finalFilled": True,
        "result": {"orderId": "1", "orderLinkId": "setup-worker-1"},
        "fillVerification": {
            "ok": True,
            "accepted": True,
            "finalFilled": True,
            "state": "filled",
            "terminal": True,
            "unresolved": False,
            "cumExecQty": "1",
            "avgPrice": "100",
            "reason": "Order is Filled with positive executed quantity.",
        },
    }


class Core:
    def __init__(
        self,
        enabled=True,
        order_result=None,
        store=None,
        order_exception=None,
    ):
        self.BOT_LOCK = threading.Lock()
        self.BOT_STATE = {
            "enabled": enabled,
            "maxOpenPositions": 3,
            "riskPerTradePct": 2,
            "maxAllocationUsdt": 1000,
            "dailyLossCapUsdt": 100,
            "maxTradesPerDay": 10,
            "lastTradeAt": 0,
        }
        self.engine = Engine()
        self.order_calls = []
        self.order_result = verified_fill_order() if order_result is None else order_result
        self.order_exception = order_exception
        self._durable_state_store = store or FakeStore()
        self.queue_probe = None

    def get_bot_engine(self):
        return self.engine

    def daily_risk_report(self, _state):
        return {"ok": True, "blocked": False, "reason": "Daily risk OK"}

    def existing_position_guard(self, _symbol, _side, _state):
        return {"ok": True, "reason": "No conflict"}

    def get_mark_price(self, _symbol):
        return 100.0

    def calculate_position_sizing(self, _symbol, _state):
        return {"ok": True, "qty": "1", "riskAmount": 20}

    def place_demo_order(self, symbol, side, qty, source, stop_loss_pct, take_profit_pct):
        if self.queue_probe is not None:
            self.queue_probe()
        self.order_calls.append((symbol, side, qty, source, stop_loss_pct, take_profit_pct))
        if self.order_exception is not None:
            raise self.order_exception
        return copy.deepcopy(self.order_result)


def candidate(now=1000, *, key="BTCUSDT:1:Buy", symbol="BTCUSDT"):
    return {
        "candidateKey": key,
        "symbol": symbol,
        "side": "Buy",
        "entryReference": 100.0,
        "stopLoss": 99.0,
        "takeProfitReference": 102.2,
        "createdAt": now,
    }


def reset_state():
    execution_handoff._STATE.update(
        {
            "status": "idle",
            "lastRunAt": 0,
            "lastCandidateKey": None,
            "lastResult": None,
            "attempts": 0,
            "executed": 0,
            "blocked": 0,
            "failed": 0,
            "lastError": None,
            "activeClaim": None,
            "claimStore": None,
        }
    )


def execute(order_result, *, store=None):
    reset_state()
    core = Core(enabled=True, order_result=order_result, store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    return core, setup, result


def active_claim(store):
    return store.get(execution_handoff._ACTIVE_CLAIM_KEY)


def last_claim(store):
    return store.get(execution_handoff._LAST_CLAIM_KEY)


def claim_states(store):
    states = []
    for action, key, value in store.history:
        if action in {"put", "put_if_absent"} and key == execution_handoff._ACTIVE_CLAIM_KEY:
            states.append(value.get("state"))
    return states


def test_stopped_bot_keeps_candidate_queued_without_creating_claim():
    reset_state()
    store = FakeStore()
    core = Core(enabled=False, store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["currentResult"]["code"] == "BOT_STOPPED"
    assert len(setup.queue) == 1
    assert core.order_calls == []
    assert active_claim(store) is None


def test_confirmed_candidate_is_claimed_before_submit_and_removed_after_resolution():
    reset_state()
    store = FakeStore()
    core = Core(enabled=True, store=store)
    setup = SetupWorker(candidate())

    def observe_queue_during_submit():
        assert len(setup.queue) == 1
        claim = active_claim(store)
        assert claim["state"] == "CLAIMED"

    core.queue_probe = observe_queue_during_submit
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["status"] == "executed"
    assert result["lastResult"]["code"] == "ORDER_FILLED"
    assert len(core.order_calls) == 1
    assert setup.queue == []
    assert active_claim(store) is None
    assert last_claim(store)["state"] == "COMPLETED"
    assert claim_states(store) == ["CLAIMED", "SUBMITTED", "RESOLVED_FILLED", "COMPLETED"]
    assert core.BOT_STATE["lastTradeAt"] == 1000


def test_queue_removal_targets_exact_candidate_not_fifo_assumption():
    reset_state()
    first = candidate(key="BTCUSDT:1:Buy")
    second = candidate(key="ETHUSDT:2:Buy", symbol="ETHUSDT")
    store = FakeStore()
    core = Core(store=store)
    setup = SetupWorker([first, second])
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "ORDER_FILLED"
    assert [row["candidateKey"] for row in setup.queue] == ["ETHUSDT:2:Buy"]


def test_retcode_zero_without_fill_verification_remains_claimed_and_fails_closed():
    order = {"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}}
    core, setup, result = execute(order)
    assert result["status"] == "error"
    assert result["lastResult"]["code"] == "FILL_VERIFICATION_MISSING"
    assert len(setup.queue) == 1
    assert active_claim(core._durable_state_store)["state"] == "UNRESOLVED"
    assert core.BOT_STATE["lastTradeAt"] == 0
    assert core.BOT_STATE["enabled"] is False


def test_legacy_accepted_only_fill_shape_is_not_sufficient():
    order = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"orderId": "1"},
        "fillVerification": {"accepted": True, "state": "filled"},
    }
    core, setup, result = execute(order)
    assert result["lastResult"]["code"] == "FILL_VERIFICATION_INCOMPLETE"
    assert len(setup.queue) == 1
    assert active_claim(core._durable_state_store)["state"] == "UNRESOLVED"
    assert core.BOT_STATE["lastTradeAt"] == 0
    assert core.BOT_STATE["enabled"] is False


@pytest.mark.parametrize(
    ("state", "terminal", "unresolved", "code"),
    [
        ("partial", False, True, "ORDER_PARTIAL"),
        ("pending", False, True, "ORDER_FILL_UNRESOLVED"),
        ("timeout", False, True, "ORDER_FILL_UNRESOLVED"),
        ("unknown", False, True, "ORDER_FILL_UNRESOLVED"),
        ("invalid_fill", True, True, "FILL_VERIFICATION_INVALID"),
        ("verification_error", False, True, "FILL_VERIFICATION_INVALID"),
    ],
)
def test_unresolved_or_partial_fill_states_keep_candidate_and_claim(state, terminal, unresolved, code):
    order = verified_fill_order()
    order["accepted"] = False
    order["finalFilled"] = False
    order["retMsg"] = f"Order not accepted as filled: {state}"
    order["fillVerification"].update(
        {
            "accepted": False,
            "finalFilled": False,
            "state": state,
            "terminal": terminal,
            "unresolved": unresolved,
            "reason": f"Fill state is {state}.",
        }
    )
    core, setup, result = execute(order)
    assert result["status"] == "error"
    assert result["lastResult"]["code"] == code
    assert len(setup.queue) == 1
    assert active_claim(core._durable_state_store)["state"] == "UNRESOLVED"
    assert core.BOT_STATE["lastTradeAt"] == 0
    assert core.BOT_STATE["enabled"] is False


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("cancelled", "ORDER_CANCELLED"),
        ("rejected", "ORDER_REJECTED"),
        ("create_rejected", "ORDER_REJECTED"),
    ],
)
def test_terminal_no_fill_is_persisted_before_candidate_removal(state, code):
    order = verified_fill_order()
    order["accepted"] = False
    order["finalFilled"] = False
    order["fillVerification"].update(
        {
            "accepted": False,
            "finalFilled": False,
            "state": state,
            "terminal": True,
            "unresolved": False,
            "cumExecQty": "0",
            "reason": f"Order ended as {state}.",
        }
    )
    core, setup, result = execute(order)
    store = core._durable_state_store
    assert result["status"] == "error"
    assert result["lastResult"]["code"] == code
    assert setup.queue == []
    assert active_claim(store) is None
    assert last_claim(store)["state"] == "COMPLETED"
    assert "RESOLVED_NO_FILL" in claim_states(store)
    assert core.BOT_STATE["lastTradeAt"] == 0
    assert core.BOT_STATE["enabled"] is True


def test_exchange_create_rejection_is_resolved_without_resubmit_risk():
    order = {"retCode": 10001, "retMsg": "Order rejected", "result": {}}
    core, setup, result = execute(order)
    assert result["lastResult"]["code"] == "ORDER_CREATE_REJECTED"
    assert setup.queue == []
    assert active_claim(core._durable_state_store) is None
    assert last_claim(core._durable_state_store)["state"] == "COMPLETED"


def test_submission_exception_keeps_candidate_and_blocks_second_submission():
    reset_state()
    store = FakeStore()
    core = Core(store=store, order_exception=TimeoutError("socket timed out"))
    setup = SetupWorker(candidate())
    first = execution_handoff.run_once(core, setup, now=1000)
    assert first["lastResult"]["code"] == "SUBMISSION_OUTCOME_UNKNOWN"
    assert len(setup.queue) == 1
    assert len(core.order_calls) == 1
    assert active_claim(store)["state"] == "SUBMISSION_UNKNOWN"
    assert core.BOT_STATE["enabled"] is False
    with core.BOT_LOCK:
        core.BOT_STATE["enabled"] = True
    second = execution_handoff.run_once(core, setup, now=1030)
    assert second["lastResult"]["code"] == "EXECUTION_CLAIM_UNRESOLVED"
    assert len(core.order_calls) == 1
    assert len(setup.queue) == 1
    assert core.BOT_STATE["enabled"] is False


def test_restart_with_claimed_state_blocks_resubmission():
    reset_state()
    existing = {
        execution_handoff._ACTIVE_CLAIM_KEY: {
            "version": 1,
            "claimId": "claim-1",
            "candidateKey": "BTCUSDT:1:Buy",
            "candidate": candidate(),
            "state": "CLAIMED",
            "claimedAt": 900,
            "updatedAt": 900,
            "queueRemovalPending": False,
        }
    }
    store = FakeStore(initial=existing)
    core = Core(store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "EXECUTION_CLAIM_UNRESOLVED"
    assert core.order_calls == []
    assert len(setup.queue) == 1
    assert core.BOT_STATE["enabled"] is False


def test_restart_after_durable_resolution_finishes_queue_removal_without_order():
    reset_state()
    resolved = {
        execution_handoff._ACTIVE_CLAIM_KEY: {
            "version": 1,
            "claimId": "claim-1",
            "candidateKey": "BTCUSDT:1:Buy",
            "candidate": candidate(),
            "state": "RESOLVED_FILLED",
            "claimedAt": 900,
            "submittedAt": 901,
            "resolvedAt": 902,
            "updatedAt": 902,
            "queueRemovalPending": True,
        }
    }
    store = FakeStore(initial=resolved)
    core = Core(store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "CLAIM_RECOVERY_COMPLETED"
    assert core.order_calls == []
    assert setup.queue == []
    assert active_claim(store) is None
    assert last_claim(store)["state"] == "COMPLETED"


def test_restart_after_queue_removal_treats_absence_as_idempotent_completion():
    reset_state()
    resolved = {
        execution_handoff._ACTIVE_CLAIM_KEY: {
            "version": 1,
            "claimId": "claim-1",
            "candidateKey": "BTCUSDT:1:Buy",
            "candidate": candidate(),
            "state": "RESOLVED_FILLED",
            "claimedAt": 900,
            "submittedAt": 901,
            "resolvedAt": 902,
            "updatedAt": 902,
            "queueRemovalPending": True,
        }
    }
    store = FakeStore(initial=resolved)
    core = Core(store=store)
    setup = SetupWorker([])
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "CLAIM_RECOVERY_COMPLETED"
    assert core.order_calls == []
    assert active_claim(store) is None


def test_atomic_claim_conflict_blocks_order_and_keeps_candidate():
    reset_state()
    store = FakeStore(conflict=True)
    core = Core(store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "EXECUTION_CLAIM_CONFLICT"
    assert core.order_calls == []
    assert len(setup.queue) == 1
    assert core.BOT_STATE["enabled"] is False


def test_degraded_store_blocks_execution_before_order():
    reset_state()
    store = FakeStore(persistent=False)
    core = Core(store=store)
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "DURABLE_CLAIM_UNAVAILABLE"
    assert core.order_calls == []
    assert len(setup.queue) == 1
    assert core.BOT_STATE["enabled"] is False


def test_missing_store_blocks_execution_before_order():
    reset_state()
    core = Core(store=FakeStore())
    del core._durable_state_store
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "DURABLE_CLAIM_UNAVAILABLE"
    assert core.order_calls == []
    assert len(setup.queue) == 1


def test_position_guard_blocks_without_creating_claim():
    reset_state()
    store = FakeStore()
    core = Core(enabled=True, store=store)
    core.existing_position_guard = lambda *_args: {"ok": False, "reason": "Max open positions reached"}
    setup = SetupWorker(candidate())
    result = execution_handoff.run_once(core, setup, now=1000)
    assert result["lastResult"]["code"] == "POSITION_GUARD_BLOCKED"
    assert len(setup.queue) == 1
    assert core.order_calls == []
    assert active_claim(store) is None


def test_stale_candidate_is_removed_without_order_or_claim():
    reset_state()
    store = FakeStore()
    core = Core(enabled=True, store=store)
    setup = SetupWorker(candidate(now=1))
    result = execution_handoff.run_once(core, setup, now=5000)
    assert result["lastResult"]["code"] == "CANDIDATE_STALE"
    assert setup.queue == []
    assert core.order_calls == []
    assert active_claim(store) is None
