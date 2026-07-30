import pytest

from backend.engines.order_fill import (
    FINAL_FILL_BLOCK_CODE,
    clear_pending_entry,
    finalize_entry_order,
    get_pending_entry,
)


ACK = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {"orderId": "terminal-order-1"},
}


def payload(status, qty):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {
                    "orderId": "terminal-order-1",
                    "orderStatus": status,
                    "cumExecQty": qty,
                    "avgPrice": "100",
                }
            ]
        },
    }


@pytest.fixture(autouse=True)
def reset_pending_entry():
    clear_pending_entry()
    yield
    clear_pending_entry()


@pytest.mark.parametrize("status", ["Cancelled", "Rejected"])
def test_terminal_status_with_executed_quantity_is_partial_unresolved(status):
    result = finalize_entry_order(
        "BTCUSDT",
        ACK,
        lambda method, path, params: payload(status, "0.03"),
        attempts=1,
        delay_seconds=0,
    )

    assert result["retCode"] == FINAL_FILL_BLOCK_CODE
    assert result["fillVerification"]["state"] == "partial"
    assert result["fillVerification"]["unresolved"] is True
    assert result["requiresOperatorReview"] is True
    assert get_pending_entry()["symbol"] == "BTCUSDT"
