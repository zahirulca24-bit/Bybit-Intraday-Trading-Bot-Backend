from backend import position_synced_server as runtime


def test_protection_error_blocks_before_original_position_guard(monkeypatch):
    original_called = False

    def original_guard(symbol, signal, state):
        nonlocal original_called
        original_called = True
        return {"ok": True}

    monkeypatch.setattr(runtime, "collect_open_positions", lambda requester: {
        "retCode": 10001,
        "retMsg": "exchange unavailable",
        "result": {"list": [], "count": 0},
    })
    monkeypatch.setattr(runtime, "_ORIGINAL_EXISTING_POSITION_GUARD", original_guard)

    result = runtime._protected_existing_position_guard("BTCUSDT", "Buy", {})

    assert result["ok"] is False
    assert result["protectionBlocked"] is True
    assert "exchange unavailable" in result["reason"]
    assert original_called is False


def test_invalid_position_payload_blocks_before_original_guard(monkeypatch):
    original_called = False

    def original_guard(symbol, signal, state):
        nonlocal original_called
        original_called = True
        return {"ok": True}

    monkeypatch.setattr(runtime, "collect_open_positions", lambda requester: {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": None},
    })
    monkeypatch.setattr(runtime, "_ORIGINAL_EXISTING_POSITION_GUARD", original_guard)

    result = runtime._protected_existing_position_guard("ETHUSDT", "Sell", {})

    assert result["ok"] is False
    assert result["positions"] == []
    assert "invalid position list" in result["reason"]
    assert original_called is False


def test_valid_protected_state_delegates_to_original_guard(monkeypatch):
    def original_guard(symbol, signal, state):
        return {"ok": True, "reason": "original guard passed", "symbol": symbol}

    monkeypatch.setattr(runtime, "collect_open_positions", lambda requester: {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "0.002",
                    "stopLoss": "63000",
                    "takeProfit": "65000",
                    "trailingStop": "",
                }
            ],
            "count": 1,
        },
    })
    monkeypatch.setattr(runtime, "_ORIGINAL_EXISTING_POSITION_GUARD", original_guard)

    result = runtime._protected_existing_position_guard("ETHUSDT", "Buy", {})

    assert result["ok"] is True
    assert result["reason"] == "original guard passed"
    assert result["symbol"] == "ETHUSDT"
