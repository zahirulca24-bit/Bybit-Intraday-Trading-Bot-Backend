import sys
import os
from unittest.mock import MagicMock

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

def test_is_authorized_with_token():
    mock_handler = MagicMock(spec=server.Handler)
    mock_handler.headers = {"Authorization": "Bearer secret_token_123"}

    orig_token = server.ADMIN_TOKEN
    try:
        # Scenario 1: ADMIN_TOKEN is set, and the header matches
        server.ADMIN_TOKEN = "secret_token_123"
        assert server.Handler.is_authorized(mock_handler) is True

        # Scenario 2: ADMIN_TOKEN is set, but header mismatches
        mock_handler.headers = {"Authorization": "Bearer wrong_token_456"}
        assert server.Handler.is_authorized(mock_handler) is False

        # Scenario 3: ADMIN_TOKEN is set, but header is completely missing
        mock_handler.headers = {}
        assert server.Handler.is_authorized(mock_handler) is False
    finally:
        server.ADMIN_TOKEN = orig_token

def test_is_authorized_without_token():
    mock_handler = MagicMock(spec=server.Handler)
    mock_handler.headers = {"Authorization": "Bearer secret_token_123"}

    orig_token = server.ADMIN_TOKEN
    try:
        # Scenario 4: ADMIN_TOKEN is not configured (empty string)
        server.ADMIN_TOKEN = ""
        assert server.Handler.is_authorized(mock_handler) is False
    finally:
        server.ADMIN_TOKEN = orig_token
