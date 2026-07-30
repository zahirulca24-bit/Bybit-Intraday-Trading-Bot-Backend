from backend.protection_verification import annotate_protection, protection_gate


def payload(rows):
    return {"retCode": 0, "retMsg": "OK", "result": {"list": rows, "count": len(rows)}}


def test_marks_position_protected_with_stop_and_take_profit():
    result = annotate_protection(payload([{"symbol": "BTCUSDT", "size": "0.002", "stopLoss": "63000", "takeProfit": "65000", "trailingStop": ""}]))
    row = result["result"]["list"][0]
    assert row["protection"]["protected"] is True
    assert result["result"]["protection"]["ok"] is True


def test_marks_position_protected_with_stop_and_trailing_stop():
    result = annotate_protection(payload([{"symbol": "BTCUSDT", "size": "0.002", "stopLoss": "63000", "takeProfit": "", "trailingStop": "64.1"}]))
    assert result["result"]["list"][0]["protection"]["protected"] is True


def test_missing_stop_loss_blocks_protection_gate():
    source = payload([{"symbol": "BTCUSDT", "size": "0.002", "stopLoss": "", "takeProfit": "65000", "trailingStop": ""}])
    ok, reason = protection_gate(source)
    assert ok is False
    assert "BTCUSDT" in reason


def test_exchange_error_fails_closed():
    ok, reason = protection_gate({"retCode": 10001, "retMsg": "exchange unavailable", "result": {}})
    assert ok is False
    assert reason == "exchange unavailable"


def test_malformed_success_payloads_fail_closed():
    for source in (
        None,
        {"retCode": 0},
        {"retCode": 0, "result": None},
        {"retCode": 0, "result": {"list": None}},
        {"retCode": 0, "result": {"list": ["invalid-row"]}},
    ):
        ok, reason = protection_gate(source)
        assert ok is False
        assert "failed" in reason


def test_empty_valid_position_list_is_safe():
    ok, reason = protection_gate(payload([]))
    assert ok is True
    assert "All open positions" in reason
