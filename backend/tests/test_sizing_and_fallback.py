import sys
import os
from unittest.mock import patch, MagicMock
from decimal import Decimal

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

def test_calculate_position_sizing_rejection():
    # Test that calculate_position_sizing rejects when quantity/notional is below minimum limit
    with patch("server.get_mark_price") as mock_mark, \
         patch("server.get_wallet_equity") as mock_equity, \
         patch("server.get_instrument_rules") as mock_rules:

        # Scenario: Mark price is 1.0, equity is 1000
        # instrument has minNotionalValue of 10.0, qtyStep of 1.0, minOrderQty of 1.0.
        # Max allocation is 5.0, so the raw quantity based on max allocation is 5.0.
        # But wait, let's say max allocation is 5.0. 5.0 * 1.0 = 5.0 notional, which is < minNotionalValue (10.0).
        # This should cause local rejection.
        mock_mark.return_value = 1.0
        mock_equity.return_value = (1000.0, "OK")
        mock_rules.return_value = {
            "ok": True,
            "reason": "OK",
            "qtyStep": Decimal("1.0"),
            "minOrderQty": Decimal("1.0"),
            "maxOrderQty": Decimal("100.0"),
            "minNotionalValue": Decimal("10.0"),
            "tickSize": Decimal("0.01"),
        }

        state = {
            "riskPerTradePct": 0.5,
            "stopLossPct": 0.8,
            "maxAllocationUsdt": 5.0,
        }

        res = server.calculate_position_sizing("ZAMAUSDT", state)

        assert res["ok"] is False
        assert "Order blocked locally: quantity/notional does not meet Bybit instrument limits." in res["reason"]
        assert res["roundedQty"] == "0"
        assert res["minQty"] == "1"
        assert res["maxQty"] == "100"
        assert res["minNotionalValue"] == "10"
        assert res["estimatedNotional"] == "0"

def test_place_demo_order_local_rejection():
    # Test that place_demo_order blocks locally when qty/notional is invalid
    with patch("server.get_mark_price") as mock_mark, \
         patch("server.get_instrument_rules") as mock_rules:

        mock_mark.return_value = 1.0
        mock_rules.return_value = {
            "ok": True,
            "reason": "OK",
            "qtyStep": Decimal("0.1"),
            "minOrderQty": Decimal("1.0"), # min qty is 1.0
            "maxOrderQty": Decimal("100.0"),
            "minNotionalValue": Decimal("5.0"),
            "tickSize": Decimal("0.01"),
        }

        # We try to place an order with qty "0.5", which is below minOrderQty (1.0)
        res = server.place_demo_order("ZAMAUSDT", "Buy", "0.5", "manual")
        assert res["retCode"] == -1001
        assert "Order blocked locally" in res["retMsg"]

def test_demo_order_fallback_logic():
    # Test that /api/bybit/demo-order handler falls back to BTCUSDT or other options when primary fails sizing
    with patch("server.calculate_position_sizing") as mock_sizing, \
         patch("server.place_demo_order") as mock_place_order, \
         patch("server.get_bot_engine") as mock_engine:

         # Mocking get_bot_engine to avoid actual journal access issues
         mock_journal = MagicMock()
         mock_engine.return_value.journal = mock_journal

         # Mock calculate_position_sizing to return False for "ZAMAUSDT" but True for "BTCUSDT"
         def side_effect_sizing(symbol, state):
             if symbol == "ZAMAUSDT":
                 return {"ok": False, "reason": "Sizing failed"}
             elif symbol == "BTCUSDT":
                 return {"ok": True, "qty": "0.005"}
             return {"ok": False, "reason": "Sizing failed"}

         mock_sizing.side_effect = side_effect_sizing
         mock_place_order.return_value = {"retCode": 0, "retMsg": "Success"}

         # Creating a mock HTTP Handler to run the routing logic
         mock_handler = MagicMock(spec=server.Handler)
         mock_handler.path = "/api/bybit/demo-order"
         mock_handler.headers = {"Authorization": "Bearer token"}

         # We also mock json_response to verify the return value
         responses = []
         def mock_json_response(handler, status, payload):
             responses.append((status, payload))

         with patch("server.json_response", mock_json_response), \
              patch("server.read_json") as mock_read_json, \
              patch.object(server.Handler, "is_authorized", return_value=True):

              mock_read_json.return_value = {
                  "confirmDemoOrder": True,
                  "symbol": "ZAMAUSDT",
                  "side": "Buy",
                  "stopLossPct": 0.8,
                  "takeProfitPct": 1.6,
              }

              server.Handler.do_POST(mock_handler)

              assert len(responses) == 1
              status, payload = responses[0]
              assert status == 200
              # It should have successfully fallback-placed BTCUSDT order
              assert mock_place_order.call_args[0][0] == "BTCUSDT"
              assert mock_place_order.call_args[0][2] == "0.005"
