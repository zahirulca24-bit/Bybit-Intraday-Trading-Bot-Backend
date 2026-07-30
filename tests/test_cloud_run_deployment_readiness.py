from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend import cloud_run_server, deployment_readiness


def valid_environment():
    return {
        "ADMIN_TOKEN": "a" * 40,
        "BYBIT_API_KEY": "bybit_demo_key_123456",
        "BYBIT_API_SECRET": "s" * 32,
        "DATABASE_URL": "postgresql://bot:secret@db.internal:5432/bybit",
        "BYBIT_BASE_URL": "https://api-demo.bybit.com",
        "CORS_ALLOWED_ORIGINS": "https://frontend.example.com",
        "PUBLIC_BASE_URL": "https://backend.example.com",
        "BOT_EXECUTION_ENABLED": "false",
        "HOST": "0.0.0.0",
        "PORT": "8080",
    }


def install_environment(monkeypatch):
    for name, value in valid_environment().items():
        monkeypatch.setenv(name, value)


def test_valid_cloud_run_environment_is_sanitized():
    env = valid_environment()
    status = deployment_readiness.environment_status(env)

    assert status["ok"] is True
    assert status["config"]["bybitBaseUrl"] == deployment_readiness.DEMO_BASE_URL
    assert status["config"]["corsOrigins"] == ["https://frontend.example.com"]
    serialized = repr(status)
    assert env["ADMIN_TOKEN"] not in serialized
    assert env["BYBIT_API_SECRET"] not in serialized
    assert env["DATABASE_URL"] not in serialized


def test_environment_rejects_live_endpoint_wildcard_and_auto_resume():
    env = valid_environment()
    env.update({
        "BYBIT_BASE_URL": "https://api.bybit.com",
        "CORS_ALLOWED_ORIGINS": "*",
        "BOT_EXECUTION_ENABLED": "true",
    })

    status = deployment_readiness.environment_status(env)

    assert status["ok"] is False
    joined = " ".join(status["errors"])
    assert "api-demo.bybit.com" in joined
    assert "must not contain '*'" in joined
    assert "must remain false" in joined


def test_runtime_readiness_accepts_leader_and_standby_api_instances(monkeypatch):
    install_environment(monkeypatch)
    core = SimpleNamespace(durable_state_status=lambda: {"ok": True, "degraded": False})
    workers = SimpleNamespace(snapshot=lambda: {"status": "running"})

    leader = deployment_readiness.runtime_readiness(
        core,
        SimpleNamespace(snapshot=lambda: {"status": "leader", "leader": True}),
        workers,
    )
    standby = deployment_readiness.runtime_readiness(
        core,
        SimpleNamespace(snapshot=lambda: {"status": "standby", "leader": False}),
        workers,
    )

    assert leader["ok"] is True
    assert leader["executionReady"] is True
    assert standby["ok"] is True
    assert standby["executionReady"] is False


def test_runtime_readiness_fails_closed_on_lost_leadership(monkeypatch):
    install_environment(monkeypatch)
    result = deployment_readiness.runtime_readiness(
        SimpleNamespace(durable_state_status=lambda: {"ok": True, "degraded": False}),
        SimpleNamespace(snapshot=lambda: {"status": "lost", "leader": False, "reason": "connection lost"}),
        SimpleNamespace(snapshot=lambda: {"status": "standby"}),
    )

    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert "connection lost" in result["reasons"]


def test_cloud_run_entrypoint_exposes_probe_contract():
    source = Path(cloud_run_server.__file__).read_text(encoding="utf-8")

    assert '"/healthz"' in source
    assert '"/readyz"' in source
    assert '"/api/health"' in source
    assert "deployment_readiness.require_environment()" in source
