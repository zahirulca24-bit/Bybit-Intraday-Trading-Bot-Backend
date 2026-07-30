from copy import deepcopy

import pytest

from backend.engines.position_management import (
    manage_positions,
    pending_partial_close,
    reset_management_state,
)


POSITION = {
    "symbol": "BTCUSDT",
    "side": "Buy",
    "size": "1",
    "avgPrice": "100",
    "markPrice": "101",
    "stopLoss": "99",
    "trailingStop": "0",
    "positionIdx": 0,
    "createdTime": "1700000000000",
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
    def __init__(self, *, reject_partial=False, apply_trailing=False):
        self.position = deepcopy(POSITION)
        self.reject_partial = reject_partial
        self.apply_trailing = apply_trailing
        self.engine = Engine()
        self.partial_posts = 0
        self.trailing_posts = 0

    def get_bot_engine(self):
        return self.engine

    def get_open_positions(self):
        return [deepcopy(self.position)], "OK"

    def get_symbol_open_positions(self, symbol):
        return [deepcopy(self.position)], "OK"

    def position_key(self, position):
        return f"{position['symbol']}:{position['side']}:{position.get('createdTime')}"

    def format_price(self, symbol, value):
        return f"{float(value):.8f}".rstrip("0").rstrip(".")

    def close_partial_position(self, position, close_pct):
        self.partial_posts += 1
        if self.reject_partial:
            return {"retCode": 110001, "retMsg": "reduce order rejected", "result": {}}
        raise AssertionError("partial close should be rejected in this test")

    def set_trailing_stop(self, position, distance_pct):
        self.trailing_posts += 1
        if self.apply_trailing:
            distance = float(position["markPrice"]) * float(distance_pct) / 100
            self.position["trailingStop"] = self.format_price(position["symbol"], distance)
        return {"retCode": 0, "retMsg": "OK", "result": {}}

    def bybit_request(self, method, path, payload):
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def clean_state():
    reset_management_state()
    yield
    reset_management_state()


def test_trailing_ack_without_exchange_state_is_not_success():
    core = Core(apply_trailing=False)
    state = {
        "partialTpEnabled": False,
        "breakevenEnabled": False,
        "trailingStopEnabled": True,
        "trailingStopTriggerPct": 0.5,
        "trailingStopDistancePct": 0.35,
    }

    result = manage_positions(core, state, attempts=1, delay_seconds=0)

    assert result["ok"] is False
    assert result["actions"][0]["verification"]["state"] == "trailing_sync_timeout"
    assert core.engine.journal.entries[0]["event"] == "trailing_stop_enabled_failed"
    assert core.engine.status["tradeManagement"] == "error"


def test_rejected_partial_close_is_failed_without_unresolved_latch():
    core = Core(reject_partial=True)
    state = {
        "partialTpEnabled": True,
        "partialTpTriggerPct": 0.5,
        "partialTpClosePct": 40,
        "breakevenEnabled": False,
        "trailingStopEnabled": False,
    }

    result = manage_positions(core, state, attempts=1, delay_seconds=0)

    assert result["ok"] is False
    assert result["actions"][0]["verification"]["state"] == "create_rejected"
    assert core.engine.journal.entries[0]["event"] == "partial_take_profit_failed"
    assert pending_partial_close() is None
