"""Cloud Run-safe entrypoint for the canonical Bybit Demo backend.

This wrapper preserves the existing secure runtime while adding:
- PostgreSQL advisory-lock single-leader ownership for automatic workers;
- fail-closed checks on order-capable mutation routes;
- periodic leader-connection verification inside the worker scheduler;
- SIGTERM/SIGINT graceful shutdown and advisory-lock release.
"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer
from typing import Any

try:
    from . import runtime_instance_guard, runtime_lifecycle, secure_server
except ImportError:
    import runtime_instance_guard
    import runtime_lifecycle
    import secure_server

core = secure_server.core

_EXECUTION_MUTATION_PATHS = {
    "/api/bybit/demo-order",
    "/api/bot/start",
    "/api/bot/manage-positions",
}

_ORIGINAL_ORCHESTRATOR_START = secure_server.runtime_orchestrator.start
_ORIGINAL_RUN_DUE_ONCE = secure_server.runtime_orchestrator.run_due_once
_PATCHED = False


def _leadership_reason() -> str:
    status = runtime_instance_guard.snapshot()
    return str(status.get("reason") or "This instance is not the automatic-execution leader.")


def _disable_execution(reason: str) -> None:
    with core.BOT_LOCK:
        core.BOT_STATE.update({
            "enabled": False,
            "runtimeExecutionLeader": False,
            "executionGuard": {"ok": False, "reason": reason},
            "lastReason": reason,
        })


def _install_orchestrator_guard() -> None:
    """Acquire leadership only after durable PostgreSQL state is installed."""
    def guarded_start(
        runtime_core: Any,
        symbol_worker: Any,
        setup_worker: Any,
        execution_handoff: Any | None = None,
    ) -> dict[str, Any]:
        leadership = runtime_instance_guard.install(runtime_core)
        if not leadership.get("leader"):
            _disable_execution(str(leadership.get("reason") or "Runtime leadership denied."))
            return {
                "status": "standby",
                "threadAlive": False,
                "runtimeLeadership": leadership,
            }
        return _ORIGINAL_ORCHESTRATOR_START(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
        )

    def guarded_run_due_once(
        runtime_core: Any,
        symbol_worker: Any,
        setup_worker: Any,
        execution_handoff: Any | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        if not runtime_instance_guard.is_leader():
            reason = _leadership_reason()
            _disable_execution(reason)
            return {
                "status": "standby",
                "threadAlive": False,
                "lastError": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
            }
        return _ORIGINAL_RUN_DUE_ONCE(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
            now=now,
        )

    secure_server.runtime_orchestrator.start = guarded_start
    secure_server.runtime_orchestrator.run_due_once = guarded_run_due_once


def _install_start_gate() -> None:
    """Prevent a follower API instance from re-enabling BOT_STATE."""
    handler = secure_server.verified.guarded.GuardedHandler
    if getattr(handler, "_cloud_run_leadership_gate_installed", False):
        return
    original_start = handler._start_bot

    def guarded_start_bot(instance: Any, payload: dict[str, Any]):
        if not runtime_instance_guard.is_leader():
            reason = _leadership_reason()
            _disable_execution(reason)
            core.json_response(instance, 503, {
                "ok": False,
                "enabled": False,
                "reason": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
            })
            return
        return original_start(instance, payload)

    handler._start_bot = guarded_start_bot
    handler._cloud_run_leadership_gate_installed = True


def install_cloud_run_safety() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _install_orchestrator_guard()
    _PATCHED = True


class CloudRunSecureHandler(secure_server.SecurePositionSyncedHandler):
    """Canonical handler with explicit runtime leadership diagnostics."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/runtime/leadership":
            if secure_server.reject_disallowed_origin(self):
                return
            if not secure_server.authorize_get(self, path):
                return
            core.json_response(self, 200, {
                "ok": True,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
                "workerRuntime": secure_server.runtime_orchestrator.snapshot(),
            })
            return
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in _EXECUTION_MUTATION_PATHS and not runtime_instance_guard.is_leader():
            if secure_server.reject_disallowed_origin(self):
                return
            if not self.is_authorized():
                core.json_response(self, 401, {"ok": False, "error": "Unauthorized"})
                return
            reason = _leadership_reason()
            _disable_execution(reason)
            core.json_response(self, 503, {
                "ok": False,
                "enabled": False,
                "reason": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
            })
            return
        return super().do_POST()


def run() -> None:
    install_cloud_run_safety()
    secure_server.install_secure_runtime()
    _install_start_gate()

    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), CloudRunSecureHandler)
    lifecycle = runtime_lifecycle.RuntimeLifecycle(
        core,
        server,
        secure_server.runtime_orchestrator,
        runtime_instance_guard,
    )
    core.runtime_lifecycle_status = lifecycle.snapshot
    lifecycle.install_signal_handlers()

    print(f"Cloud Run-safe Bybit Demo backend listening on http://{host}:{port}", flush=True)
    print(f"Runtime leadership: {runtime_instance_guard.snapshot()}", flush=True)
    print(f"Durable state: {core.durable_state_status()}", flush=True)
    print(f"Worker runtime: {secure_server.runtime_orchestrator.snapshot()}", flush=True)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        lifecycle.finalize()


if __name__ == "__main__":
    run()
