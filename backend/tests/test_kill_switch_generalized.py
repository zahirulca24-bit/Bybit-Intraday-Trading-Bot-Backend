import sys
import os
from unittest.mock import patch, MagicMock

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

def test_kill_switch_with_no_positions():
    # Test that kill switch behaves correctly when there are no open positions
    with patch("server.get_open_positions") as mock_get_open:
        mock_get_open.return_value = ([], "OK")

        mock_handler = MagicMock(spec=server.Handler)
        mock_handler.path = "/api/bybit/kill-switch"
        mock_handler.headers = {"Authorization": "Bearer token"}

        responses = []
        def mock_json_response(handler, status, payload):
            responses.append((status, payload))

        # Capture print statements to verify required logging "No open positions to close"
        printed_msgs = []
        def mock_print(*args, **kwargs):
            printed_msgs.append(" ".join(map(str, args)))

        with patch("server.json_response", mock_json_response), \
             patch("server.read_json") as mock_read_json, \
             patch("builtins.print", mock_print), \
             patch.object(server.Handler, "is_authorized", return_value=True):

             mock_read_json.return_value = {"symbol": "BTCUSDT"}

             # Set bot enabled state to True
             with server.BOT_LOCK:
                 server.BOT_STATE["enabled"] = True

             server.Handler.do_POST(mock_handler)

             # Bot should be disabled
             assert server.BOT_STATE["enabled"] is False

             assert len(responses) == 1
             status, payload = responses[0]
             assert status == 200
             assert payload["retCode"] == -1
             assert payload["retMsg"] == "No open positions to close."
             assert payload["closedSymbols"] == []
             assert payload["closeAttempts"] == 0
             assert payload["openPositionsBefore"] == 0
             assert "No open positions to close" in printed_msgs

def test_kill_switch_with_positions():
    # Test that kill switch closes active positions and journals correctly with actual symbol
    with patch("server.get_open_positions") as mock_get_open, \
         patch("server.bybit_request") as mock_bybit_req, \
         patch("server.close_symbol_positions") as mock_close, \
         patch("server.get_bot_engine") as mock_engine:

        mock_get_open.return_value = ([
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.01"}
        ], "OK")

        mock_bybit_req.return_value = {"retCode": 0, "retMsg": "OK"}
        mock_close.return_value = {"ok": True, "orders": [{"retCode": 0, "result": {"orderId": "123"}}]}

        # Mock journal to verify it receives the correct symbol
        mock_journal = MagicMock()
        mock_engine.return_value.journal = mock_journal

        mock_handler = MagicMock(spec=server.Handler)
        mock_handler.path = "/api/bybit/kill-switch"
        mock_handler.headers = {"Authorization": "Bearer token"}

        responses = []
        def mock_json_response(handler, status, payload):
            responses.append((status, payload))

        printed_msgs = []
        def mock_print(*args, **kwargs):
            printed_msgs.append(" ".join(map(str, args)))

        with patch("server.json_response", mock_json_response), \
             patch("server.read_json") as mock_read_json, \
             patch("builtins.print", mock_print), \
             patch.object(server.Handler, "is_authorized", return_value=True):

             mock_read_json.return_value = {"symbol": "BEATUSDT"} # Scanner selected BEATUSDT

             # Set bot enabled state to True
             with server.BOT_LOCK:
                 server.BOT_STATE["enabled"] = True

             server.Handler.do_POST(mock_handler)

             # Bot should be disabled
             assert server.BOT_STATE["enabled"] is False

             assert len(responses) == 1
             status, payload = responses[0]
             assert status == 200
             assert payload["retCode"] == 0
             assert payload["closedSymbols"] == ["BTCUSDT"]
             assert payload["closeAttempts"] == 1
             assert payload["openPositionsBefore"] == 1

             # Logs must match exactly: "Kill switch closing BTCUSDT" and "Closed BTCUSDT position"
             assert "Kill switch closing BTCUSDT" in printed_msgs
             assert "Closed BTCUSDT position" in printed_msgs

             # Check that the trade journal received BTCUSDT (not the scanner selected symbol)
             mock_journal.add.assert_called_with("kill_switch", {
                 "symbol": "BTCUSDT",
                 "cancelResult": {"retCode": 0, "retMsg": "OK"},
                 "closeResult": {"ok": True, "orders": [{"retCode": 0, "result": {"orderId": "123"}}]}
             })
