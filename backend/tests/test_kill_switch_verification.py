from __future__ import annotations

from backend.kill_switch_verification import execute_verified_kill_switch


def position(symbol: str, size: str = "1", side: str = "Buy") -> dict:
    return {"symbol": symbol, "size": size, "side": side}


def accepted_order() -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "123"}}


def test_kill_switch_reports_success_only_after_positions_are_flat() -> None:
    snapshots = [
        ([position("BTCUSDT")], "OK"),
        ([], "OK"),
    ]
    journal = []

    result = execute_verified_kill_switch(
        get_open_positions=lambda: snapshots.pop(0),
        cancel_all=lambda symbol: {"retCode": 0, "symbol": symbol},
        close_symbol_positions=lambda symbol: {"ok": True, "orders": [accepted_order()]},
        journal_add=lambda event, payload: journal.append((event, payload)),
        verify_delay_seconds=0,
    )

    assert result["retCode"] == 0
    assert result["verifiedFlat"] is True
    assert result["remainingPositions"] == []
    assert result["closedSymbols"] == ["BTCUSDT"]
    assert result["closeAttempts"] == 1
    assert journal[0][0] == "kill_switch"


def test_kill_switch_fails_when_position_remains_open() -> None:
    snapshots = [
        ([position("ETHUSDT", "2")], "OK"),
        ([position("ETHUSDT", "1")], "OK"),
        ([position("ETHUSDT", "1")], "OK"),
        ([position("ETHUSDT", "1")], "OK"),
    ]

    result = execute_verified_kill_switch(
        get_open_positions=lambda: snapshots.pop(0),
        cancel_all=lambda symbol: {"retCode": 0},
        close_symbol_positions=lambda symbol: {"ok": True, "orders": [accepted_order()]},
        journal_add=lambda event, payload: None,
        verify_delay_seconds=0,
    )

    assert result["retCode"] == -1
    assert result["verifiedFlat"] is False
    assert result["closedSymbols"] == []
    assert result["remainingPositions"] == [
        {"symbol": "ETHUSDT", "side": "Buy", "size": 1.0}
    ]
    assert "remain open" in result["retMsg"]


def test_kill_switch_fails_on_partial_order_rejection_even_when_flat() -> None:
    snapshots = [
        ([position("BTCUSDT"), position("SOLUSDT")], "OK"),
        ([], "OK"),
    ]

    def close(symbol: str) -> dict:
        if symbol == "SOLUSDT":
            return {
                "ok": True,
                "orders": [{"retCode": 10001, "retMsg": "rejected", "result": {}}],
            }
        return {"ok": True, "orders": [accepted_order()]}

    result = execute_verified_kill_switch(
        get_open_positions=lambda: snapshots.pop(0),
        cancel_all=lambda symbol: {"retCode": 0},
        close_symbol_positions=close,
        journal_add=lambda event, payload: None,
        verify_delay_seconds=0,
    )

    assert result["retCode"] == -1
    assert result["verifiedFlat"] is True
    assert result["closedSymbols"] == []
    assert "rejected" in result["retMsg"]


def test_kill_switch_fails_closed_when_final_position_fetch_fails() -> None:
    snapshots = [
        ([position("BTCUSDT")], "OK"),
        (None, "Bybit unavailable"),
        (None, "Bybit unavailable"),
        (None, "Bybit unavailable"),
    ]

    result = execute_verified_kill_switch(
        get_open_positions=lambda: snapshots.pop(0),
        cancel_all=lambda symbol: {"retCode": 0},
        close_symbol_positions=lambda symbol: {"ok": True, "orders": [accepted_order()]},
        journal_add=lambda event, payload: None,
        verify_delay_seconds=0,
    )

    assert result["retCode"] == -1
    assert result["verifiedFlat"] is False
    assert "verification failed" in result["retMsg"]


def test_kill_switch_treats_already_flat_account_as_verified_success() -> None:
    result = execute_verified_kill_switch(
        get_open_positions=lambda: ([], "OK"),
        cancel_all=lambda symbol: (_ for _ in ()).throw(AssertionError("not called")),
        close_symbol_positions=lambda symbol: (_ for _ in ()).throw(AssertionError("not called")),
        journal_add=lambda event, payload: None,
    )

    assert result["retCode"] == 0
    assert result["verifiedFlat"] is True
    assert result["openPositionsBefore"] == 0
