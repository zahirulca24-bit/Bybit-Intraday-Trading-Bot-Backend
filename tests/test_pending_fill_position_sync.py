import pytest

from backend import position_synced_server as runtime
from backend.engines.order_fill import (
    clear_pending_entry,
    finalize_entry_order,
    get_pending_entry,
)


ACK = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {"orderId": "order-sync-1"},
}


def status_payload(status, qty):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {
                    "orderId": "order-sync-1",
                    "orderStatus": status,
                    "cumExecQty": qty,
                    "avgPrice": "100",
                }
            ]
        },
    }


@pytest.fixture(autouse=True)
def reset_pending_latch():
    clear_pending_entry()
    yield
    clear_pending_entry()


def test_delayed_fill_remains_blocked_until_non_zero_position_is_synchronized(monkeypatch):
    finalize_entry_order(
        "BTCUSDT",
        ACK,
        lambda method, path, params: status_payload("PartiallyFilled", "0.02"),
        attempts=1,
        delay_seconds=0,
    )
    assert get_pending_entry() is not None

    monkeypatch.setattr(
        runtime.guarded.core,
        "bybit_request",
        lambda method, path, params: status_payload("Filled", "0.1"),
    )
    monkeypatch.setattr(
        runtime,
        "collect_open_positions",
        lambda requester: {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": [], "count": 0},
        },
    )

    allowed, reason, verification = runtime._pending_fill_guard()
    assert allowed is False
    assert verification["state"] == "filled"
    assert "not synchronized" in reason
    assert get_pending_entry() is not None

    monkeypatch.setattr(
        runtime,
        "collect_open_positions",
        lambda requester: {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.1",
                    }
                ],
                "count": 1,
            },
        },
    )

    allowed, reason, verification = runtime._pending_fill_guard()
    assert allowed is False
    assert verification["state"] == "filled"
    assert "position-synchronized" in reason
    assert get_pending_entry() is None
