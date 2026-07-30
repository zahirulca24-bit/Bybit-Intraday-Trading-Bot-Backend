import sys
import os
import time
from unittest.mock import patch, MagicMock

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

def test_check_and_reset_daily_state():
    # Initial state mimicking a locked setup from yesterday
    yesterday_date = "2020-01-01"
    state = {
        "tradingDateKey": yesterday_date,
        "lastReason": "Daily loss cap reached. Trading locked for today.",
        "executionGuard": {
            "ok": False,
            "reason": "Daily loss cap reached. Trading locked for today."
        },
        "orderLifecycle": {
            "signal": "Buy",
            "guard": "blocked",
            "order": "skipped",
            "protection": "skipped",
            "status": "blocked",
            "reason": "Daily loss cap reached."
        },
        "dailyRisk": {
            "tradesToday": 6,
            "lossUsed": 27.83,
            "dailyLossUsed": 27.83,
            "blocked": True,
            "reason": "Daily loss cap reached."
        }
    }

    # Mock the current date key helper to return today's date instead of yesterday's
    today_date = "2020-01-02"
    with patch("server.get_current_trading_date_key", return_value=today_date):
        server.check_and_reset_daily_state(state)

    # Verify transition and reset
    assert state["tradingDateKey"] == today_date
    assert state["dailyRisk"]["tradesToday"] == 0
    assert state["dailyRisk"]["lossUsed"] == 0.0
    assert state["dailyRisk"]["dailyLossUsed"] == 0.0
    assert state["dailyRisk"]["blocked"] is False

    # Verify locks and reasons are unlocked
    assert "New trading day started" in state["lastReason"]
    assert state["executionGuard"]["ok"] is True
    assert "New trading day started" in state["executionGuard"]["reason"]
    assert state["orderLifecycle"]["status"] == "idle"


def test_count_today_accepted_orders():
    # Setup mock journal entries
    date_key = "2020-01-02"
    start_epoch = server.get_trading_day_start_epoch(date_key)

    mock_journal = MagicMock()
    mock_journal.entries = [
        # Yesterday's trade
        {
            "time": start_epoch - 3600,
            "event": "auto_order",
            "payload": {
                "result": {"retCode": 0}
            }
        },
        # Today's accepted auto trade
        {
            "time": start_epoch + 10,
            "event": "auto_order",
            "payload": {
                "result": {"retCode": 0}
            }
        },
        # Today's rejected auto trade
        {
            "time": start_epoch + 20,
            "event": "auto_order",
            "payload": {
                "result": {"retCode": -1, "retMsg": "Insufficient margin"}
            }
        },
        # Today's accepted manual trade
        {
            "time": start_epoch + 30,
            "event": "manual_order",
            "payload": {
                "result": {"retCode": 0}
            }
        },
        # Today's blocked manual trade
        {
            "time": start_epoch + 40,
            "event": "manual_order",
            "payload": {
                "result": {"retCode": -1001, "retMsg": "Sizing rules blocked locally"}
            }
        }
    ]

    mock_engine = MagicMock()
    mock_engine.journal = mock_journal

    # Should count only today's accepted auto and manual orders (exactly 2)
    count = server.count_today_accepted_orders(mock_engine, date_key)
    assert count == 2


def test_get_daily_closed_pnl_filtering():
    date_key = "2020-01-02"
    start_epoch = server.get_trading_day_start_epoch(date_key)
    start_ms = start_epoch * 1000

    # Mock journal entries in mock engine
    mock_journal = MagicMock()
    mock_journal.entries = [
        # Today's accepted auto trade 1 (BTCUSDT)
        {
            "time": start_epoch + 10,
            "event": "auto_order",
            "payload": {
                "symbol": "BTCUSDT",
                "signal": "Buy",
                "result": {
                    "retCode": 0,
                    "result": {
                        "orderId": "entry_order_1",
                        "orderLinkId": "link_entry_1"
                    }
                }
            }
        },
        # Today's accepted auto trade 2 (ETHUSDT)
        {
            "time": start_epoch + 20,
            "event": "auto_order",
            "payload": {
                "symbol": "ETHUSDT",
                "signal": "Sell",
                "result": {
                    "retCode": 0,
                    "result": {
                        "orderId": "entry_order_2",
                        "orderLinkId": "link_entry_2"
                    }
                }
            }
        }
    ]
    mock_engine = MagicMock()
    mock_engine.journal = mock_journal

    def mock_bybit_request_fn(method, path, params=None):
        params = params or {}
        if path == "/v5/position/closed-pnl":
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        # Yesterday's closed trade
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "close_yesterday",
                            "updatedTime": str(start_ms - 5000),
                            "closedPnl": "-10.0"
                        },
                        # Today's closed trade 1 (loss)
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "close_order_1",
                            "updatedTime": str(start_ms + 1000),
                            "closedPnl": "-15.5"
                        },
                        # Today's closed trade 2 (profit)
                        {
                            "symbol": "ETHUSDT",
                            "orderId": "close_order_2",
                            "updatedTime": str(start_ms + 2000),
                            "closedPnl": "5.0"
                        }
                    ]
                }
            }
        elif path == "/v5/execution/list":
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        # Entry executions
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "entry_order_1",
                            "orderLinkId": "link_entry_1",
                            "execType": "Trade",
                            "execTime": str(start_ms + 10),
                            "closedSize": "0"
                        },
                        {
                            "symbol": "ETHUSDT",
                            "orderId": "entry_order_2",
                            "orderLinkId": "link_entry_2",
                            "execType": "Trade",
                            "execTime": str(start_ms + 20),
                            "closedSize": "0"
                        },
                        # Closing executions
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "close_order_1",
                            "execType": "Trade",
                            "execTime": str(start_ms + 1000),
                            "closedSize": "0.001"
                        },
                        {
                            "symbol": "ETHUSDT",
                            "orderId": "close_order_2",
                            "execType": "Trade",
                            "execTime": str(start_ms + 2000),
                            "closedSize": "0.1"
                        }
                    ]
                }
            }
        return {"retCode": -1, "retMsg": "Mock not set", "result": {}}

    with patch("server.get_bot_engine", return_value=mock_engine), \
         patch("server.bybit_request", side_effect=mock_bybit_request_fn):
        pnl, msg = server.get_daily_closed_pnl(date_key)

    assert pnl == -10.5
    assert msg == "OK"
