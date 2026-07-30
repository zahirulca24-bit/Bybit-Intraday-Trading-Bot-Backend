from copy import deepcopy

import pytest

from backend import position_synced_server as runtime
from backend.engines.position_management import (
    entry_gate,
    manage_positions,
    pending_partial_close,
    position_key,
    reset_management_state,
)


POSITION = {
    "symbol": "BTCUSDT",
    "side": "Buy",
    "size": "1",
    "avgPrice": "100",
    "markPrice": "101",
    "stopLoss": "99",
    "takeProfit": "103",
    "trailingStop": "0",
    "positionIdx": 0,
    "createdTime": "1700000000000",
    "updatedTime": "1700000000100",
}


class Journal:
    def __init__(self):
        self.entries = []

    def add(self, event, payload):
        self.entries.append({"event": event, "payload": payload})


class Engine:
    def __init__(self):
        self.journal = Journal()
        self.status = {}

    def set_status(self, name, value):
        self.status[name] = value


class Core:
    def __init__(
        self,
        *,
        order_status="Filled",
        executed_qty="0.4",
        apply_stop=True,
        apply_trailing=True,
        partial_ret_code=0,
    ):
        self.position = deepcopy(POSITION)
        self.order_status = order_status
        self.executed_qty = executed_qty
        self.apply_stop = apply_stop
        self.apply_trailing = apply_trailing
        self.partial_ret_code = partial_ret_code
        self.engine = Engine()
        self.stop_posts = 0
        self.trailing_posts = 0
        self.partial_posts = 0

    def get_bot_engine(self):
        return self.engine

    def get_open_positions(self):
        return ([deepcopy(self.position)] if self.position else []), "OK"

    def get_symbol_open_positions(self, symbol):
        return ([deepcopy(self.position)] if self.position else []), "OK"

    def position_key(self, position):
        return f"{position['symbol']}:{position['side']}:{position.get('updatedTime')}"

    def format_price(self, symbol, value):
        text = f"{float(value):.8f}".rstrip("0").rstrip(".")
        return text or "0"

    def close_partial_position(self, position, close_pct):
        self.partial_posts += 1
        if self.partial_ret_code != 0:
            return {"retCode": self.partial_ret_code, "retMsg": "close rejected", "result": {}}
        if self.order_status == "Filled" and float(self.executed_qty) > 0:
            remaining = max(0.0, float(self.position["size"]) - float(self.executed_qty))
            self.position = None if remaining == 0 else {**self.position, "size": str(remaining)}
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "partial-1", "orderLinkId": "partial-link-1"},
        }

    def set_trailing_stop(self, position, distance_pct):
        self.trailing_posts += 1
        if self.apply_trailing:
            distance = float(position["markPrice"]) * float(distance_pct) / 100
            self.position["trailingStop"] = self.format_price(position["symbol"], distance)
        return {"retCode": 0, "retMsg": "OK", "result": {}}

    def bybit_request(self, method, path, payload):
        if method == "POST" and path == "/v5/position/trading-stop":
            self.stop_posts += 1
            if self.apply_stop:
                self.position["stopLoss"] = payload["stopLoss"]
            return {"retCode": 0, "retMsg": "OK", "result": {}}
        if method == "GET" and path in {"/v5/order/realtime", "/v5/order/history"}:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "orderId": "partial-1",
                            "orderLinkId": "partial-link-1",
                            "orderStatus": self.order_status,
                            "cumExecQty": self.executed_qty,
                            "avgPrice": "101",
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def clean_management_state():
    reset_management_state()
    yield
    reset_management_state()


def state_for(action):
    state = {
        "partialTpEnabled": False,
        "breakevenEnabled": False,
        "trailingStopEnabled": False,
        "positionManagementRetrySeconds": 60,
    }
    if action == "partial":
        state.update({"partialTpEnabled": True, "partialTpTriggerPct": 0.5, "partialTpClosePct": 40})
    elif action == "breakeven":
        state.update({"breakevenEnabled": True, "breakevenTriggerPct": 0.5})
    elif action == "trailing":
        state.update({"trailingStopEnabled": True, "trailingStopTriggerPct": 0.5, "trailingStopDistancePct": 0.35})
    return state


def test_position_key_does_not_change_with_updated_time():
    first = position_key(POSITION)
    changed = {**POSITION, "updatedTime": "9999999999999", "markPrice": "110"}
    assert position_key(changed) == first


def test_breakeven_is_journaled_only_after_exchange_position_verification():
    core = Core(apply_stop=True)

    first = manage_positions(core, state_for("breakeven"), attempts=1, delay_seconds=0)
    # Simulate a stale exchange snapshot; verified journal state must prevent a duplicate POST.
    core.position["stopLoss"] = "99"
    second = manage_positions(core, state_for("breakeven"), attempts=1, delay_seconds=0)

    assert first["ok"] is True
    assert first["actions"][0]["verified"] is True
    assert core.engine.journal.entries[0]["event"] == "breakeven_stop"
    assert core.stop_posts == 1
    assert second["actions"][0]["status"] == "skipped"


def test_unverified_breakeven_is_failure_and_immediate_retry_is_suppressed():
    core = Core(apply_stop=False)

    first = manage_positions(core, state_for("breakeven"), attempts=1, delay_seconds=0)
    second = manage_positions(core, state_for("breakeven"), attempts=1, delay_seconds=0)

    assert first["ok"] is False
    assert first["actions"][0]["verification"]["state"] == "stop_sync_timeout"
    assert core.engine.journal.entries[0]["event"] == "breakeven_stop_failed"
    assert core.engine.status["tradeManagement"] == "error"
    assert core.stop_posts == 1
    assert second["actions"][0]["status"] == "skipped"


def test_trailing_stop_requires_reported_exchange_state():
    core = Core(apply_trailing=True)
    result = manage_positions(core, state_for("trailing"), attempts=1, delay_seconds=0)

    assert result["ok"] is True
    assert result["actions"][0]["verification"]["state"] == "trailing_verified"
    assert core.engine.journal.entries[0]["event"] == "trailing_stop_enabled"


def test_partial_take_profit_requires_full_fill_and_position_reduction():
    core = Core(order_status="Filled", executed_qty="0.4")
    result = manage_positions(core, state_for("partial"), attempts=1, delay_seconds=0)

    assert result["ok"] is True
    assert result["actions"][0]["verification"]["state"] == "position_reduced"
    assert result["actions"][0]["verification"]["fill"]["accepted"] is True
    assert core.engine.journal.entries[0]["event"] == "partial_take_profit"
    assert pending_partial_close() is None


def test_partial_or_unknown_close_blocks_new_entries_without_false_success():
    core = Core(order_status="PartiallyFilledCanceled", executed_qty="0.2")
    result = manage_positions(core, state_for("partial"), attempts=1, delay_seconds=0)
    allowed, reason, verification = entry_gate(core)

    assert result["ok"] is False
    assert result["actions"][0]["verified"] is False
    assert core.engine.journal.entries[0]["event"] == "partial_take_profit_failed"
    assert pending_partial_close() is not None
    assert allowed is False
    assert "unresolved" in reason
    assert verification["fill"]["state"] == "partial"


def test_runtime_guard_checks_position_management_latch_first(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "position_management_entry_gate",
        lambda core: (False, "partial close unresolved", {"state": "partial"}),
    )

    def must_not_continue(*args, **kwargs):
        raise AssertionError("later position/protection checks must not run")

    monkeypatch.setattr(runtime, "collect_open_positions", must_not_continue)
    result = runtime._protected_existing_position_guard("ETHUSDT", "Buy", {})

    assert result["ok"] is False
    assert result["positionManagementBlocked"] is True
    assert result["positionManagementVerification"]["state"] == "partial"
