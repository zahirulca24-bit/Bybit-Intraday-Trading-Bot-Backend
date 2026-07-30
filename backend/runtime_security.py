"""Authentication and CORS policy for the canonical HTTP runtime.

The browser UI and public market-data endpoints remain reachable without an
admin token. Every other ``/api/`` GET endpoint is protected by default, so a
new sensitive route cannot accidentally become public.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any


PUBLIC_GET_PATHS = frozenset(
    {
        "/api/health",
        "/api/bybit/ticker",
        "/api/bybit/kline",
        "/api/bot/universe",
        "/api/bot/scanner",
        "/api/bot/replay",
    }
)


def _normalized_origin(value: Any) -> str | None:
    text = str(value or "").strip().rstrip("/")
    if not text or text.lower() == "null":
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def configured_origins() -> set[str]:
    values: list[str] = []
    # Canonical name plus the Render dashboard name already used in production.
    values.extend(os.environ.get("CORS_ALLOWED_ORIGINS", "").split(","))
    values.extend(os.environ.get("CORS_ORIGINS", "").split(","))
    values.append(os.environ.get("RENDER_EXTERNAL_URL", ""))
    values.append(os.environ.get("PUBLIC_BASE_URL", ""))
    return {
        normalized
        for value in values
        if (normalized := _normalized_origin(value)) is not None
    }


def request_origin(handler: Any) -> str | None:
    return _normalized_origin(handler.headers.get("Origin", ""))


def request_base_origin(handler: Any) -> str | None:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "")
    scheme = forwarded_proto.split(",", 1)[0].strip().lower() or "http"
    forwarded_host = handler.headers.get("X-Forwarded-Host", "")
    host = forwarded_host.split(",", 1)[0].strip() or handler.headers.get("Host", "")
    return _normalized_origin(f"{scheme}://{host}") if host else None


def origin_allowed(handler: Any) -> bool:
    raw_origin = handler.headers.get("Origin", "")
    if not raw_origin:
        return True
    origin = request_origin(handler)
    if origin is None:
        return False
    same_origin = request_base_origin(handler)
    return origin == same_origin or origin in configured_origins()


def get_requires_auth(path: str) -> bool:
    clean_path = str(path or "").split("?", 1)[0]
    return clean_path.startswith("/api/") and clean_path not in PUBLIC_GET_PATHS


def _send_cors_headers(handler: Any) -> None:
    origin = request_origin(handler)
    if origin and origin_allowed(handler):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )
    handler.send_header("Access-Control-Max-Age", "600")


def secure_json_response(handler: Any, status: int, payload: Any) -> None:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    _send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def reject_disallowed_origin(handler: Any) -> bool:
    if origin_allowed(handler):
        return False
    secure_json_response(
        handler,
        403,
        {"ok": False, "error": "Origin not allowed"},
    )
    return True


def authorize_get(handler: Any, path: str) -> bool:
    if not get_requires_auth(path):
        return True
    if handler.is_authorized():
        return True
    secure_json_response(
        handler,
        401,
        {"ok": False, "error": "Unauthorized"},
    )
    return False


def handle_options(handler: Any) -> None:
    if reject_disallowed_origin(handler):
        return
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    _send_cors_headers(handler)
    handler.end_headers()


def install(core: Any) -> None:
    """Install the secure response helper used by all canonical handlers."""
    core.json_response = secure_json_response
