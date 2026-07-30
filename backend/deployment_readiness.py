"""Cloud Run environment and runtime readiness checks.

The module never exposes secret values. Configuration is validated only when
``require_environment`` or ``environment_status`` is called, so importing the
runtime remains safe for build-time smoke tests.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Mapping

DEMO_BASE_URL = "https://api-demo.bybit.com"
_REQUIRED_SECRETS = (
    "ADMIN_TOKEN",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "DATABASE_URL",
)
_PLACEHOLDER_PARTS = (
    "changeme",
    "change-me",
    "replace-me",
    "replace_me",
    "your-",
    "your_",
    "example",
    "placeholder",
    "dummy",
)


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or any(part in lowered for part in _PLACEHOLDER_PARTS)


def _valid_database_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and bool(parsed.hostname)
        and bool(parsed.path and parsed.path != "/")
    )


def _validate_origins(raw: str) -> tuple[list[str], list[str]]:
    accepted: list[str] = []
    errors: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            errors.append("CORS_ALLOWED_ORIGINS must not contain '*'.")
            continue
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            errors.append(f"Invalid CORS origin: {origin!r}.")
            continue
        host = (parsed.hostname or "").lower()
        local = host in {"localhost", "127.0.0.1", "::1"}
        if not host or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            errors.append(f"CORS origin must contain scheme and host only: {origin!r}.")
            continue
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            errors.append(f"CORS origin must use HTTPS outside localhost: {origin!r}.")
            continue
        accepted.append(origin)
    if not accepted:
        errors.append("CORS_ALLOWED_ORIGINS must contain at least one frontend origin.")
    return list(dict.fromkeys(accepted)), errors


def environment_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a sanitized startup configuration decision."""
    env = os.environ if environ is None else environ
    errors: list[str] = []
    warnings: list[str] = []

    for name in _REQUIRED_SECRETS:
        value = _value(env, name)
        if _looks_placeholder(value):
            errors.append(f"{name} is missing or still contains a placeholder value.")

    admin_token = _value(env, "ADMIN_TOKEN")
    if admin_token and len(admin_token) < 32:
        errors.append("ADMIN_TOKEN must contain at least 32 characters.")

    api_secret = _value(env, "BYBIT_API_SECRET")
    if api_secret and len(api_secret) < 16:
        errors.append("BYBIT_API_SECRET is unexpectedly short.")

    database_url = _value(env, "DATABASE_URL")
    if database_url and not _valid_database_url(database_url):
        errors.append("DATABASE_URL must be a PostgreSQL URL with host and database name.")

    base_url = _value(env, "BYBIT_BASE_URL") or DEMO_BASE_URL
    if base_url.rstrip("/") != DEMO_BASE_URL:
        errors.append(f"BYBIT_BASE_URL must be exactly {DEMO_BASE_URL}.")

    host = _value(env, "HOST") or "0.0.0.0"
    if host != "0.0.0.0":
        errors.append("HOST must be 0.0.0.0 for Cloud Run.")

    raw_port = _value(env, "PORT") or "8080"
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        errors.append("PORT must be an integer between 1 and 65535.")

    execution_flag = (_value(env, "BOT_EXECUTION_ENABLED") or "false").lower()
    if execution_flag not in {"false", "0", "no", "off"}:
        errors.append("BOT_EXECUTION_ENABLED must remain false at process startup.")

    origins, origin_errors = _validate_origins(_value(env, "CORS_ALLOWED_ORIGINS"))
    errors.extend(origin_errors)

    public_base_url = _value(env, "PUBLIC_BASE_URL")
    if not public_base_url:
        warnings.append("PUBLIC_BASE_URL is not set; add the final Cloud Run HTTPS URL after deployment.")

    return {
        "ok": not errors,
        "demoOnly": True,
        "errors": errors,
        "warnings": warnings,
        "config": {
            "host": host,
            "port": port,
            "bybitBaseUrl": base_url.rstrip("/"),
            "corsOrigins": origins,
            "publicBaseUrlConfigured": bool(public_base_url),
            "startupExecutionEnabled": False,
            "requiredSecretsConfigured": {
                name: bool(_value(env, name)) and not _looks_placeholder(_value(env, name))
                for name in _REQUIRED_SECRETS
            },
        },
    }


def require_environment(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    status = environment_status(environ)
    if not status["ok"]:
        raise RuntimeError("Cloud Run startup configuration rejected: " + " ".join(status["errors"]))
    return status


def liveness_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "alive",
        "service": "bybit-intraday-trading-bot-backend",
        "demoOnly": True,
    }


def _public_durable_status(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(status.get("ok")),
        "backend": str(status.get("backend") or "postgresql"),
        "degraded": bool(status.get("degraded")),
        "restartSafe": bool(status.get("restartSafe")),
        "persistentPathConfigured": bool(status.get("persistentPathConfigured")),
        "migrationVersion": int(status.get("migrationVersion") or 0),
    }


def _public_leadership_status(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(status.get("status") or "unknown"),
        "leader": bool(status.get("leader")),
    }


def _public_worker_status(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(status.get("status") or "unknown"),
        "threadAlive": bool(status.get("threadAlive")),
    }


def runtime_readiness(core: Any, instance_guard: Any, orchestrator: Any) -> dict[str, Any]:
    """Return public API readiness without exposing internal connection errors."""
    environment = environment_status()
    try:
        durable_internal = dict(core.durable_state_status() or {})
    except Exception:
        durable_internal = {"ok": False, "degraded": True}
    try:
        leadership_internal = dict(instance_guard.snapshot() or {})
    except Exception:
        leadership_internal = {"status": "error", "leader": False}
    try:
        workers_internal = dict(orchestrator.snapshot() or {})
    except Exception:
        workers_internal = {"status": "error", "threadAlive": False}

    durable = _public_durable_status(durable_internal)
    leadership = _public_leadership_status(leadership_internal)
    workers = _public_worker_status(workers_internal)

    durable_ready = bool(durable["ok"]) and not bool(durable["degraded"])
    api_instance_ready = leadership["status"] in {"leader", "standby"}
    ready = bool(environment.get("ok")) and durable_ready and api_instance_ready

    reasons: list[str] = []
    if not environment.get("ok"):
        reasons.extend(environment.get("errors") or [])
    if not durable_ready:
        reasons.append("Persistent PostgreSQL state is not ready.")
    if not api_instance_ready:
        reasons.append("Runtime leadership state is not ready.")

    return {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "demoOnly": True,
        "executionLeader": leadership["leader"],
        "executionReady": ready and leadership["leader"],
        "reasons": reasons,
        "environment": environment,
        "durableState": durable,
        "runtimeLeadership": leadership,
        "workerRuntime": workers,
    }
