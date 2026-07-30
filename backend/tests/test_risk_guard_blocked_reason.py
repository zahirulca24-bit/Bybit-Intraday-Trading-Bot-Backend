import sys
import os
from unittest.mock import patch, MagicMock

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server


def test_normalize_block_reason():
    # Test that normalize_block_reason maps core strings correctly
    assert server.normalize_block_reason("cooldown active") == "cooldown active"
    assert server.normalize_block_reason("Some Cooldown Detected") == "cooldown active"
    assert server.normalize_block_reason("Max Open Positions reached (1/1)") == "max open positions reached"
    assert server.normalize_block_reason("Existing BTCUSDT Buy position detected; duplicate entry blocked") == "position already open"
    assert server.normalize_block_reason("Daily loss cap reached ($30.00/$25.00)") == "daily loss cap reached"
    assert server.normalize_block_reason("No executable signal found") == "no executable signal"
    assert server.normalize_block_reason("Position not confirmed after order") == "position not confirmed after order"
    assert server.normalize_block_reason("Open order or margin hold detected") == "open order or margin hold detected"
    assert server.normalize_block_reason("Random other reason") == "Random other reason"


@patch("server.print")
@patch("server.daily_risk_report")
@patch("server.daily_loss_cap_reached")
@patch("server.evaluate_signal")
def test_bot_tick_daily_loss_cap_block(mock_evaluate, mock_reached, mock_daily_risk, mock_print):
    # Setup mock to trigger daily risk / loss cap block
    mock_reached.return_value = (True, "Daily loss cap reached. Trading locked for today.")
    mock_daily_risk.return_value = {
        "ok": True,
        "blocked": True,
        "reason": "Daily loss cap reached ($27.83/$25.00)",
        "lossUsed": 27.83,
        "dailyLossCapUsdt": 25.0,
        "tradesToday": 1,
        "maxTradesPerDay": 6,
    }
    mock_evaluate.return_value = ("Buy", "Approved by Trend Follow", [], {}, {}, {})

    # Reset BOT_STATE values to test correct setting
    with server.BOT_LOCK:
        server.BOT_STATE["enabled"] = True
        server.BOT_STATE["symbol"] = "BTCUSDT"
        server.BOT_STATE["autoPick"] = False

    server.bot_tick()

    # Verify BOT_STATE was updated with correct blocked reason
    with server.BOT_LOCK:
        assert server.BOT_STATE["executionGuard"]["ok"] is False
        assert server.BOT_STATE["executionGuard"]["reason"] == "daily loss cap reached"
        assert server.BOT_STATE["orderLifecycle"]["guard"] == "blocked"
        assert server.BOT_STATE["orderLifecycle"]["reason"] == "daily loss cap reached"

    # Verify standard output has exact reason logged
    mock_print.assert_any_call("Guard blocked: daily loss cap reached", flush=True)


@patch("server.print")
@patch("server.daily_loss_cap_reached")
@patch("server.evaluate_signal")
def test_bot_tick_no_executable_signal(mock_evaluate, mock_reached, mock_print):
    # Setup mock for WAIT signal
    mock_reached.return_value = (False, "Daily risk OK")
    mock_evaluate.return_value = ("WAIT", "Router waiting for signal", [], {}, {}, {})

    # Reset BOT_STATE values
    with server.BOT_LOCK:
        server.BOT_STATE["enabled"] = True
        server.BOT_STATE["symbol"] = "BTCUSDT"
        server.BOT_STATE["autoPick"] = False

    server.bot_tick()

    # Verify standard output logged "no executable signal"
    mock_print.assert_any_call("Guard blocked: no executable signal", flush=True)


@patch("server.print")
@patch("server.daily_risk_report")
@patch("server.daily_loss_cap_reached")
@patch("server.evaluate_signal")
@patch("server.calculate_position_sizing")
def test_bot_tick_sizing_block(mock_sizing, mock_evaluate, mock_reached, mock_daily_risk, mock_print):
    # Setup mock to trigger sizing block
    mock_reached.return_value = (False, "Daily risk OK")
    mock_daily_risk.return_value = {"ok": True, "blocked": False, "reason": "Daily risk OK"}
    mock_evaluate.return_value = ("Buy", "Approved by Trend Follow", [], {}, {}, {})
    mock_sizing.return_value = {
        "ok": False,
        "reason": "Order blocked locally: quantity/notional does not meet Bybit instrument limits."
    }

    # Reset BOT_STATE values
    with server.BOT_LOCK:
        server.BOT_STATE["enabled"] = True
        server.BOT_STATE["symbol"] = "BTCUSDT"
        server.BOT_STATE["autoPick"] = False

    server.bot_tick()

    # Verify BOT_STATE was updated with correct blocked reason
    with server.BOT_LOCK:
        assert server.BOT_STATE["executionGuard"]["ok"] is False
        # The reason is original/unaltered for sizing block or mapped
        assert "quantity/notional" in server.BOT_STATE["executionGuard"]["reason"]
        assert server.BOT_STATE["orderLifecycle"]["guard"] == "blocked"

    # Verify standard output has logged
    mock_print.assert_any_call("Guard blocked: Order blocked locally: quantity/notional does not meet Bybit instrument limits.", flush=True)


@patch("server.print")
@patch("server.daily_risk_report")
@patch("server.daily_loss_cap_reached")
@patch("server.evaluate_signal")
@patch("server.calculate_position_sizing")
@patch("server.existing_position_guard")
def test_bot_tick_position_already_open_block(mock_guard, mock_sizing, mock_evaluate, mock_reached, mock_daily_risk, mock_print):
    # Setup mock to trigger duplicate entry/position already open block
    mock_reached.return_value = (False, "Daily risk OK")
    mock_daily_risk.return_value = {"ok": True, "blocked": False, "reason": "Daily risk OK"}
    mock_evaluate.return_value = ("Buy", "Approved by Trend Follow", [], {}, {}, {})
    mock_sizing.return_value = {"ok": True, "qty": "0.001"}
    mock_guard.return_value = {
        "ok": False,
        "reason": "Existing BTCUSDT Buy position detected; duplicate entry blocked"
    }

    # Reset BOT_STATE values
    with server.BOT_LOCK:
        server.BOT_STATE["enabled"] = True
        server.BOT_STATE["symbol"] = "BTCUSDT"
        server.BOT_STATE["autoPick"] = False

    server.bot_tick()

    # Verify BOT_STATE was updated with correct normalized blocked reason
    with server.BOT_LOCK:
        assert server.BOT_STATE["executionGuard"]["ok"] is False
        assert server.BOT_STATE["executionGuard"]["reason"] == "position already open"
        assert server.BOT_STATE["orderLifecycle"]["guard"] == "blocked"
        assert server.BOT_STATE["orderLifecycle"]["reason"] == "position already open"

    # Verify standard output logged
    mock_print.assert_any_call("Guard blocked: position already open", flush=True)


@patch("server.print")
@patch("server.daily_risk_report")
@patch("server.daily_loss_cap_reached")
@patch("server.evaluate_signal")
@patch("server.calculate_position_sizing")
@patch("server.existing_position_guard")
@patch("server.get_bot_engine")
def test_bot_tick_cooldown_active_block(mock_engine, mock_guard, mock_sizing, mock_evaluate, mock_reached, mock_daily_risk, mock_print):
    # Setup mock to trigger cooldown active block
    mock_reached.return_value = (False, "Daily risk OK")
    mock_daily_risk.return_value = {"ok": True, "blocked": False, "reason": "Daily risk OK"}
    mock_evaluate.return_value = ("Buy", "Approved by Trend Follow", [], {}, {}, {})
    mock_sizing.return_value = {"ok": True, "qty": "0.001"}
    mock_guard.return_value = {"ok": True, "reason": "No existing position conflict"}

    dummy_engine = MagicMock()
    dummy_engine.risk_check.return_value = (False, "Cooldown active")
    dummy_engine.status = {}
    mock_engine.return_value = dummy_engine

    # Reset BOT_STATE values
    with server.BOT_LOCK:
        server.BOT_STATE["enabled"] = True
        server.BOT_STATE["symbol"] = "BTCUSDT"
        server.BOT_STATE["autoPick"] = False

    server.bot_tick()

    # Verify BOT_STATE was updated with correct normalized blocked reason
    with server.BOT_LOCK:
        assert server.BOT_STATE["executionGuard"]["ok"] is False
        assert server.BOT_STATE["executionGuard"]["reason"] == "cooldown active"
        assert server.BOT_STATE["orderLifecycle"]["guard"] == "blocked"
        assert server.BOT_STATE["orderLifecycle"]["reason"] == "cooldown active"

    # Verify standard output logged
    mock_print.assert_any_call("Guard blocked: cooldown active", flush=True)
