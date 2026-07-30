import io
from types import SimpleNamespace

import pytest

from backend import runtime_security as security


class FakeHandler:
    def __init__(self, headers=None, *, authorized=False):
        self.headers = headers or {}
        self._authorized = authorized
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()
        self.ended = False

    def is_authorized(self):
        return self._authorized

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        self.ended = True

    def header(self, name):
        values = [value for key, value in self.response_headers if key.lower() == name.lower()]
        return values[-1] if values else None


@pytest.fixture(autouse=True)
def clear_origin_env(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def test_public_market_routes_do_not_require_authentication():
    for path in (
        "/api/health",
        "/api/bybit/ticker",
        "/api/bybit/kline",
        "/api/bot/universe",
        "/api/bot/scanner",
        "/api/bot/replay",
    ):
        assert security.get_requires_auth(path) is False


def test_sensitive_and_future_api_routes_are_protected_by_default():
    for path in (
        "/api/bybit/wallet",
        "/api/bybit/positions",
        "/api/bybit/open-orders",
        "/api/bot/status",
        "/api/bot/sizing",
        "/api/bot/debug-risk",
        "/api/bot/engine",
        "/api/bot/journal",
        "/api/future/private-route",
    ):
        assert security.get_requires_auth(path) is True


def test_unauthorized_sensitive_get_fails_closed():
    handler = FakeHandler(authorized=False)

    assert security.authorize_get(handler, "/api/bybit/wallet") is False
    assert handler.status == 401
    assert b'"error":"Unauthorized"' in handler.wfile.getvalue()
    assert handler.header("Cache-Control") == "no-store"


def test_authorized_sensitive_get_is_allowed():
    handler = FakeHandler(authorized=True)

    assert security.authorize_get(handler, "/api/bot/journal") is True
    assert handler.status is None


def test_same_origin_request_is_allowed_without_wildcard_cors():
    handler = FakeHandler(
        {
            "Origin": "https://bot.example.com",
            "Host": "bot.example.com",
            "X-Forwarded-Proto": "https",
        }
    )

    assert security.origin_allowed(handler) is True
    security.secure_json_response(handler, 200, {"ok": True})
    assert handler.header("Access-Control-Allow-Origin") == "https://bot.example.com"
    assert handler.header("Access-Control-Allow-Origin") != "*"
    assert handler.header("Vary") == "Origin"


def test_explicit_allowlist_origin_is_allowed(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://admin.example.com, http://localhost:5173/",
    )
    handler = FakeHandler(
        {
            "Origin": "https://admin.example.com",
            "Host": "bot.example.com",
            "X-Forwarded-Proto": "https",
        }
    )

    assert security.origin_allowed(handler) is True


def test_foreign_origin_is_rejected_without_cors_permission():
    handler = FakeHandler(
        {
            "Origin": "https://evil.example",
            "Host": "bot.example.com",
            "X-Forwarded-Proto": "https",
        }
    )

    assert security.reject_disallowed_origin(handler) is True
    assert handler.status == 403
    assert handler.header("Access-Control-Allow-Origin") is None
    assert b'"error":"Origin not allowed"' in handler.wfile.getvalue()


def test_null_origin_is_rejected():
    handler = FakeHandler(
        {
            "Origin": "null",
            "Host": "bot.example.com",
            "X-Forwarded-Proto": "https",
        }
    )

    assert security.origin_allowed(handler) is False


def test_allowed_preflight_echoes_exact_origin_and_authorization_header():
    handler = FakeHandler(
        {
            "Origin": "https://bot.example.com",
            "Host": "bot.example.com",
            "X-Forwarded-Proto": "https",
        }
    )

    security.handle_options(handler)
    assert handler.status == 204
    assert handler.header("Access-Control-Allow-Origin") == "https://bot.example.com"
    assert "Authorization" in handler.header("Access-Control-Allow-Headers")
    assert handler.header("Access-Control-Allow-Origin") != "*"


def test_install_replaces_legacy_json_response():
    core = SimpleNamespace(json_response=lambda *args: None)

    security.install(core)
    assert core.json_response is security.secure_json_response
