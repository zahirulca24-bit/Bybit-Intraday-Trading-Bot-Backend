from backend import position_synced_server as runtime


def _order(state, *, accepted=False, unresolved=False, qty="0"):
    return {
        "retCode": 0 if accepted else -1007,
        "result": {"orderId": "order-1"},
        "fillVerification": {
            "accepted": accepted,
            "finalFilled": accepted,
            "state": state,
            "terminal": state in {"filled", "cancelled", "rejected", "partial"},
            "unresolved": unresolved,
            "cumExecQty": qty,
            "reason": f"verification state {state}",
        },
    }


def _run_with_state(monkeypatch, order):
    with runtime.guarded.core.BOT_LOCK:
        snapshot = dict(runtime.guarded.core.BOT_STATE)
        previous_order = runtime.guarded.core.BOT_STATE.get("lastOrder")
        runtime.guarded.core.BOT_STATE.update(
            {
                "lastOrder": previous_order,
                "enabled": True,
                "lastSignal": "Buy",
            }
        )

    def base_tick():
        with runtime.guarded.core.BOT_LOCK:
            runtime.guarded.core.BOT_STATE["lastOrder"] = order
            return dict(runtime.guarded.core.BOT_STATE)

    monkeypatch.setattr(runtime, "_BASE_BOT_TICK", base_tick)
    try:
        return runtime._fill_aware_bot_tick()
    finally:
        with runtime.guarded.core.BOT_LOCK:
            runtime.guarded.core.BOT_STATE.clear()
            runtime.guarded.core.BOT_STATE.update(snapshot)


def test_verified_fill_is_exposed_as_filled_lifecycle(monkeypatch):
    result = _run_with_state(
        monkeypatch,
        _order("filled", accepted=True, unresolved=False, qty="0.1"),
    )

    assert result["enabled"] is True
    assert result["orderLifecycle"]["order"] == "filled"
    assert result["orderLifecycle"]["status"] == "filled"
    assert result["orderLifecycle"]["protection"] == "attached"
    assert "fully filled" in result["lastReason"]


def test_partial_fill_is_separate_and_pauses_auto_engine(monkeypatch):
    result = _run_with_state(
        monkeypatch,
        _order("partial", accepted=False, unresolved=True, qty="0.04"),
    )

    assert result["enabled"] is False
    assert result["orderLifecycle"]["order"] == "partial"
    assert result["orderLifecycle"]["status"] == "partial"
    assert result["orderLifecycle"]["protection"] == "blocked"
    assert result["executionGuard"]["ok"] is False


def test_cancelled_order_is_not_reported_as_filled_or_partial(monkeypatch):
    result = _run_with_state(
        monkeypatch,
        _order("cancelled", accepted=False, unresolved=False),
    )

    assert result["enabled"] is True
    assert result["orderLifecycle"]["order"] == "cancelled"
    assert result["orderLifecycle"]["status"] == "cancelled"
    assert result["orderLifecycle"]["protection"] == "skipped"
