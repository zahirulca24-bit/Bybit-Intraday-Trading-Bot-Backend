import pytest

from backend import position_synced_server as runtime
from backend.engines.bot_engine import BotEngineV2
from backend.engines.order_fill import (
    FINAL_FILL_BLOCK_CODE,
    clear_pending_entry,
    finalize_entry_order,
    get_pending_entry,
    pending_entry_gate,
    verify_final_fill,
)


ACK = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {"orderId": "order-1", "orderLinkId": "link-1"},
}


def status_payload(status, qty="0", *, order_id="order-1"):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {
                    "orderId": order_id,
                    "orderLinkId": "link-1",
                    "orderStatus": status,
                    "cumExecQty": qty,
                    "avgPrice": "100",
                }
            ]
        },
    }


@pytest.fixture(autouse=True)
def reset_pending_fill_latch():
    clear_pending_entry()
    yield
    clear_pending_entry()


def test_new_order_must_progress_to_filled_with_positive_execution():
    realtime_calls = 0
    sleeps = []

    def requester(method, path, params):
        nonlocal realtime_calls
        if path == "/v5/order/realtime":
            realtime_calls += 1
            if realtime_calls == 1:
                return status_payload("New")
            return status_payload("Filled", "0.1")
        return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}

    result = verify_final_fill(
        "BTCUSDT",
        ACK,
        requester,
        attempts=3,
        delay_seconds=0.01,
        sleeper=sleeps.append,
    )

    assert result["accepted"] is True
    assert result["finalFilled"] is True
    assert result["state"] == "filled"
    assert result["cumExecQty"] == "0.1"
    assert result["attempts"] == 2
    assert sleeps == [0.01]


def test_filled_without_positive_executed_quantity_is_not_accepted():
    result = verify_final_fill(
        "BTCUSDT",
        ACK,
        lambda method, path, params: status_payload("Filled", "0"),
        attempts=1,
        delay_seconds=0,
    )

    assert result["accepted"] is False
    assert result["state"] == "invalid_fill"
    assert result["unresolved"] is True


def test_cancelled_order_returns_local_non_fill_code_without_latch():
    final = finalize_entry_order(
        "BTCUSDT",
        ACK,
        lambda method, path, params: status_payload("Cancelled"),
        attempts=1,
        delay_seconds=0,
    )

    assert final["retCode"] == FINAL_FILL_BLOCK_CODE
    assert final["exchangeRetCode"] == 0
    assert final["finalFilled"] is False
    assert final["fillVerification"]["state"] == "cancelled"
    assert final["requiresOperatorReview"] is False
    assert get_pending_entry() is None


def test_partial_fill_registers_global_fail_closed_latch():
    requester = lambda method, path, params: status_payload(
        "PartiallyFilledCanceled", "0.04"
    )
    final = finalize_entry_order(
        "BTCUSDT",
        ACK,
        requester,
        attempts=1,
        delay_seconds=0,
    )

    assert final["retCode"] == FINAL_FILL_BLOCK_CODE
    assert final["fillVerification"]["state"] == "partial"
    assert final["requiresOperatorReview"] is True
    assert get_pending_entry()["symbol"] == "BTCUSDT"

    allowed, reason, verification = pending_entry_gate(requester)
    assert allowed is False
    assert "unresolved" in reason
    assert verification["state"] == "partial"


def test_pending_latch_clears_only_after_terminal_resolution():
    partial = lambda method, path, params: status_payload("PartiallyFilled", "0.02")
    finalize_entry_order(
        "BTCUSDT",
        ACK,
        partial,
        attempts=1,
        delay_seconds=0,
    )
    assert get_pending_entry() is not None

    allowed, reason, verification = pending_entry_gate(
        lambda method, path, params: status_payload("Cancelled", "0")
    )
    assert allowed is True
    assert "resolved without a fill" in reason
    assert verification["state"] == "cancelled"
    assert get_pending_entry() is None


class FakeJournal:
    def __init__(self):
        self.entries = []

    def add(self, event, payload):
        self.entries.append({"event": event, "payload": payload})


class FakeTradeManagement:
    def __init__(self, status, qty):
        self.status = status
        self.qty = qty

    def place_order(self, symbol, side, qty, source, stop_loss_pct, take_profit_pct):
        return dict(ACK)

    def bybit_request(self, method, path, params):
        return status_payload(self.status, self.qty)


def build_engine(status, qty):
    engine = BotEngineV2.__new__(BotEngineV2)
    engine.status = {
        "marketData": "idle",
        "indicator": "idle",
        "strategy": "idle",
        "router": "idle",
        "risk": "idle",
        "tradeManagement": "idle",
        "journal": "idle",
    }
    engine.trade_management = FakeTradeManagement(status, qty)
    engine.journal = FakeJournal()
    return engine


def test_auto_engine_journals_success_only_after_verified_fill():
    engine = build_engine("Filled", "0.1")
    state = {
        "symbol": "BTCUSDT",
        "qty": "0.1",
        "stopLossPct": 1,
        "takeProfitPct": 2,
    }

    result = engine.execute(state, "Buy")

    assert result["retCode"] == 0
    assert result["finalFilled"] is True
    assert engine.status["tradeManagement"] == "ok"
    journal_result = engine.journal.entries[0]["payload"]["result"]
    assert journal_result["fillVerification"]["state"] == "filled"


def test_auto_engine_does_not_accept_partial_terminal_fill():
    engine = build_engine("PartiallyFilledCanceled", "0.04")
    state = {
        "symbol": "BTCUSDT",
        "qty": "0.1",
        "stopLossPct": 1,
        "takeProfitPct": 2,
    }

    result = engine.execute(state, "Buy")

    assert result["retCode"] == FINAL_FILL_BLOCK_CODE
    assert result["finalFilled"] is False
    assert result["requiresOperatorReview"] is True
    assert engine.status["tradeManagement"] == "error"
    assert engine.journal.entries[0]["payload"]["result"]["retCode"] == FINAL_FILL_BLOCK_CODE


def test_canonical_guard_blocks_before_position_checks_when_fill_is_unresolved(monkeypatch):
    requester = lambda method, path, params: status_payload(
        "PartiallyFilledCanceled", "0.04"
    )
    finalize_entry_order(
        "BTCUSDT",
        ACK,
        requester,
        attempts=1,
        delay_seconds=0,
    )

    monkeypatch.setattr(runtime.guarded.core, "bybit_request", requester)

    def must_not_collect(_requester):
        raise AssertionError("position synchronization must not run before fill latch")

    monkeypatch.setattr(runtime, "collect_open_positions", must_not_collect)

    result = runtime._protected_existing_position_guard("ETHUSDT", "Buy", {})

    assert result["ok"] is False
    assert result["fillVerificationBlocked"] is True
    assert result["fillVerification"]["state"] == "partial"
