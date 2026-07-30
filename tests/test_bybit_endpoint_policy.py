from __future__ import annotations

import importlib

import pytest

from backend import bybit_endpoint_policy as policy


class FakeCore:
    def __init__(self, environ):
        self.environ = environ
        self.calls = []
        self.config = lambda: {
            "api_key": "demo-key",
            "api_secret": "demo-secret",
            "base_url": environ.get("BYBIT_BASE_URL", "https://unsafe.invalid"),
        }

        def request(method, path, params=None):
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "params": params,
                    "base_url": self.config()["base_url"],
                }
            )
            return {"retCode": 0, "retMsg": "OK", "result": {}}

        self.bybit_request = request


def test_exact_demo_origin_is_accepted():
    assert policy.validate_demo_origin("https://api-demo.bybit.com") == policy.DEMO_API_ORIGIN


@pytest.mark.parametrize(
    "unsafe_origin",
    [
        "https://api.bybit.com",
        "https://api-testnet.bybit.com",
        "https://example.com",
        "https://127.0.0.1",
        "http://api-demo.bybit.com",
        "https://api-demo.bybit.com/v5",
        "https://user:pass@api-demo.bybit.com",
        "https://api-demo.bybit.com/",
        "https://api-demo.bybit.com?mode=demo",
        "https://api-demo.bybit.com#demo",
        " https://api-demo.bybit.com",
        "https://api-demo.bybit.com ",
    ],
)
def test_unsafe_origins_are_rejected(unsafe_origin):
    with pytest.raises(policy.DemoEndpointPolicyError):
        policy.validate_demo_origin(unsafe_origin)


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_unset_or_blank_origin_resolves_only_to_immutable_demo(configured):
    assert policy.validate_demo_origin(configured) == policy.DEMO_API_ORIGIN


def test_policy_rejects_malformed_url():
    with pytest.raises(policy.DemoEndpointPolicyError):
        policy.validate_demo_origin("https://api-demo.bybit.com:invalid")


def test_install_locks_config_and_authenticated_request_to_demo_origin():
    environ = {"BYBIT_BASE_URL": policy.DEMO_API_ORIGIN}
    core = FakeCore(environ)

    status = policy.install(core, environ)
    result = core.bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    assert status["approvedOrigin"] == policy.DEMO_API_ORIGIN
    assert status["environmentOverrideAllowed"] is False
    assert core.config()["base_url"] == policy.DEMO_API_ORIGIN
    assert result["retCode"] == 0
    assert core.calls == [
        {
            "method": "GET",
            "path": "/v5/account/wallet-balance",
            "params": {"accountType": "UNIFIED"},
            "base_url": policy.DEMO_API_ORIGIN,
        }
    ]


def test_later_environment_mutation_fails_closed_before_request():
    environ = {"BYBIT_BASE_URL": policy.DEMO_API_ORIGIN}
    core = FakeCore(environ)
    policy.install(core, environ)

    environ["BYBIT_BASE_URL"] = "https://api.bybit.com"

    with pytest.raises(policy.DemoEndpointPolicyError):
        core.bybit_request("POST", "/v5/order/create", {"category": "linear"})
    assert core.calls == []


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "https://api.bybit.com/v5/order/create",
        "//api.bybit.com/v5/order/create",
        "v5/order/create",
        "/v5/order/create?category=linear",
        "/v5/order/create#fragment",
    ],
)
def test_authenticated_request_path_cannot_bypass_approved_origin(unsafe_path):
    environ = {"BYBIT_BASE_URL": policy.DEMO_API_ORIGIN}
    core = FakeCore(environ)
    policy.install(core, environ)

    with pytest.raises(policy.DemoEndpointPolicyError):
        core.bybit_request("POST", unsafe_path, {})
    assert core.calls == []


def test_install_is_idempotent_and_remains_locked():
    environ = {"BYBIT_BASE_URL": policy.DEMO_API_ORIGIN}
    core = FakeCore(environ)

    first = policy.install(core, environ)
    locked_request = core.bybit_request
    second = policy.install(core, environ)

    assert first == second
    assert core.bybit_request is locked_request
    assert core.config()["base_url"] == policy.DEMO_API_ORIGIN


def test_canonical_secure_runtime_imports_demo_endpoint_policy(monkeypatch):
    monkeypatch.setenv("BYBIT_BASE_URL", policy.DEMO_API_ORIGIN)
    secure_server = importlib.import_module("backend.secure_server")

    assert secure_server.bybit_endpoint_policy.DEMO_API_ORIGIN == policy.DEMO_API_ORIGIN
    assert callable(secure_server.install_secure_runtime)
