"""Fail-closed endpoint policy for authenticated Bybit traffic.

The bot is intentionally Demo-only.  This module provides the single approved
origin and installs a guard at the shared ``core.config`` / ``core.bybit_request``
boundary used by the canonical runtime.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping, MutableMapping
from typing import Any


DEMO_API_ORIGIN = "https://api-demo.bybit.com"
DEMO_API_HOST = "api-demo.bybit.com"


class DemoEndpointPolicyError(RuntimeError):
    """Raised when configuration attempts to leave the Bybit Demo origin."""


def validate_demo_origin(value: str | None) -> str:
    """Return the immutable Demo origin or raise for every unsafe value.

    An unset or blank environment value cannot select another endpoint and is
    therefore resolved to the immutable Demo origin.  Any non-blank configured
    value must be the exact approved origin.
    """

    if value is None or not str(value).strip():
        return DEMO_API_ORIGIN

    raw = str(value)
    if raw != raw.strip():
        raise DemoEndpointPolicyError(
            "BYBIT_BASE_URL must exactly match the approved Bybit Demo origin."
        )

    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise DemoEndpointPolicyError("BYBIT_BASE_URL is malformed.") from exc

    if parsed.scheme != "https":
        raise DemoEndpointPolicyError("Authenticated Bybit traffic requires HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise DemoEndpointPolicyError("Credentials are not allowed in BYBIT_BASE_URL.")
    if parsed.hostname != DEMO_API_HOST or parsed.netloc != DEMO_API_HOST:
        raise DemoEndpointPolicyError(
            "Authenticated Bybit traffic is restricted to the Demo API hostname."
        )
    if port is not None:
        raise DemoEndpointPolicyError("Custom ports are not allowed in BYBIT_BASE_URL.")
    if parsed.path or parsed.query or parsed.fragment:
        raise DemoEndpointPolicyError(
            "BYBIT_BASE_URL must be an origin only, without path, query, or fragment."
        )
    if raw != DEMO_API_ORIGIN:
        raise DemoEndpointPolicyError(
            "BYBIT_BASE_URL must exactly match the approved Bybit Demo origin."
        )
    return DEMO_API_ORIGIN


def configured_demo_origin(environ: Mapping[str, str] | None = None) -> str:
    """Resolve and validate the configured origin without permitting fallback hosts."""

    source = os.environ if environ is None else environ
    return validate_demo_origin(source.get("BYBIT_BASE_URL"))


def validate_authenticated_path(path: str) -> str:
    """Reject absolute, scheme-relative, or query-bearing request paths."""

    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise DemoEndpointPolicyError("Authenticated Bybit request path is invalid.")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise DemoEndpointPolicyError("Authenticated Bybit request path is invalid.")
    return path


def policy_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return non-secret policy state for health and audit reporting."""

    origin = configured_demo_origin(environ)
    return {
        "installed": True,
        "mode": "BYBIT_DEMO_ONLY",
        "approvedOrigin": origin,
        "environmentOverrideAllowed": False,
    }


def install(core: Any, environ: MutableMapping[str, str] | None = None) -> dict[str, Any]:
    """Install the Demo-only policy at the shared authenticated-client boundary.

    The configured value is revalidated on every config/request call.  A later
    environment mutation therefore fails closed instead of redirecting traffic.
    """

    source = os.environ if environ is None else environ
    configured_demo_origin(source)

    if getattr(core, "_bybit_demo_endpoint_policy_installed", False):
        return policy_status(source)

    original_config = core.config
    original_bybit_request = core.bybit_request

    def locked_config() -> dict[str, Any]:
        configured_demo_origin(source)
        cfg = dict(original_config())
        cfg["base_url"] = DEMO_API_ORIGIN
        return cfg

    def locked_bybit_request(method: str, path: str, params: Any = None):
        configured_demo_origin(source)
        validate_authenticated_path(path)
        cfg = locked_config()
        if cfg.get("base_url") != DEMO_API_ORIGIN:
            raise DemoEndpointPolicyError(
                "Authenticated Bybit request was blocked by the Demo-only policy."
            )
        return original_bybit_request(method, path, params)

    core.config = locked_config
    core.bybit_request = locked_bybit_request
    core.BYBIT_DEMO_API_ORIGIN = DEMO_API_ORIGIN
    core._bybit_demo_endpoint_policy_installed = True
    return policy_status(source)
