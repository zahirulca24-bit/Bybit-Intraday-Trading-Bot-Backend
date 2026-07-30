import sys
import os
from unittest.mock import patch, MagicMock

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

def test_daily_loss_cap_reached_helper():
    # Test that the daily_loss_cap_reached helper behaves correctly
    with patch("server.get_daily_closed_pnl") as mock_pnl:
        state = {"dailyLossCapUsdt": 25.0}

        # Case 1: Closed PnL is 0.0 (no loss)
        mock_pnl.return_value = (0.0, "OK")
        reached, reason = server.daily_loss_cap_reached(state)
        assert reached is False

        # Case 2: Closed PnL is -10.0 (loss is 10.0, which is < 25.0 cap)
        mock_pnl.return_value = (-10.0, "OK")
        reached, reason = server.daily_loss_cap_reached(state)
        assert reached is False

        # Case 3: Closed PnL is -27.83 (loss is 27.83, which is >= 25.0 cap)
        mock_pnl.return_value = (-27.83, "OK")
        reached, reason = server.daily_loss_cap_reached(state)
        assert reached is True
        assert reason == "Daily loss cap reached. Trading locked for today."

def test_bot_start_blocked_by_daily_loss_cap():
    # Test that starting the bot is blocked when daily loss cap is reached
    with patch("server.get_daily_closed_pnl") as mock_pnl:
        mock_pnl.return_value = (-27.83, "OK")

        # Mocking the Handler and json_response to check the block
        mock_handler = MagicMock(spec=server.Handler)
        mock_handler.path = "/api/bot/start"
        mock_handler.headers = {"Authorization": "Bearer token"}

        responses = []
        def mock_json_response(handler, status, payload):
            responses.append((status, payload))

        with patch("server.json_response", mock_json_response), \
             patch("server.read_json") as mock_read_json, \
             patch.object(server.Handler, "is_authorized", return_value=True):

             mock_read_json.return_value = {
                 "dailyLossCapUsdt": 25.0
             }

             server.Handler.do_POST(mock_handler)

             assert len(responses) == 1
             status, payload = responses[0]
             assert status == 200
             assert payload["ok"] is False
             assert payload["enabled"] is False
             assert payload["reason"] == "Daily loss cap reached. Trading locked for today."

def test_demo_order_blocked_by_daily_loss_cap():
    # Test that /api/bybit/demo-order blocks order placement when daily loss cap is reached
    with patch("server.get_daily_closed_pnl") as mock_pnl:
        mock_pnl.return_value = (-27.83, "OK")

        # Mocking the Handler and json_response to check the block
        mock_handler = MagicMock(spec=server.Handler)
        mock_handler.path = "/api/bybit/demo-order"
        mock_handler.headers = {"Authorization": "Bearer token"}

        responses = []
        def mock_json_response(handler, status, payload):
            responses.append((status, payload))

        with patch("server.json_response", mock_json_response), \
             patch("server.read_json") as mock_read_json, \
             patch.object(server.Handler, "is_authorized", return_value=True):

             # Set cap in BOT_STATE
             with server.BOT_LOCK:
                 server.BOT_STATE["dailyLossCapUsdt"] = 25.0

             mock_read_json.return_value = {
                 "confirmDemoOrder": True,
                 "symbol": "BTCUSDT",
                 "side": "Buy",
             }

             server.Handler.do_POST(mock_handler)

             assert len(responses) == 1
             status, payload = responses[0]
             assert status == 200
             assert payload["ok"] is False
             assert payload["retCode"] == -1
             assert payload["retMsg"] == "Daily loss cap reached. Trading locked for today."
