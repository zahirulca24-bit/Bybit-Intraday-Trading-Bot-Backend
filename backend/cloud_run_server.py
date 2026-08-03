"""Cloud Run-safe entrypoint for the canonical Bybit Demo backend.

This wrapper preserves the existing secure runtime while adding:
- validated fail-fast Cloud Run configuration;
- public liveness and readiness probes with no secret values;
- PostgreSQL advisory-lock single-leader ownership for automatic workers;
- fail-closed checks on order-capable mutation routes;
- periodic leader-connection verification inside the worker scheduler;
- standby-to-leader promotion after deployment overlap ends;
- SIGTERM/SIGINT graceful shutdown and advisory-lock release.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The legacy backend contains a few absolute imports such as
# ``from engines...`` even when loaded as the ``backend`` package.  Make both
# the repository root and backend directory explicit before importing the
# canonical patch chain.  This keeps CI, Cloud Run, and direct execution
# deterministic without masking nested ImportError exceptions.
_BACKEND_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPOSITORY_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

if __package__:
    from . import (
        deployment_readiness,
        runtime_instance_guard,
        runtime_lifecycle,
        secure_server,
    )
else:
    import deployment_readiness
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
_PROMOTION_LOCK = threading.RLock()
_PROMOTION_THREAD: threading.Thread | None = None


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


def _shutdown_started(runtime_core: Any) -> bool:
    reader = getattr(runtime_core, "runtime_lifecycle_status", None)
    if not callable(reader):
        return False
    try:
        return bool((reader() or {}).get("shutdownStarted"))
    except Exception:
        return False


def _promote_from_standby_once(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> bool:
    """Retry the existing advisory lock and start workers exactly once on promotion."""
    leadership = runtime_instance_guard.acquire(runtime_core)
    if not leadership.get("leader"):
        _disable_execution(str(leadership.get("reason") or "Runtime leadership denied."))
        return False
    _ORIGINAL_ORCHESTRATOR_START(
        runtime_core,
        symbol_worker,
        setup_worker,
        execution_handoff,
    )
    return True


def _start_standby_promotion(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> None:
    """Use the existing orchestrator idle cadence to recover after revision overlap."""
    global _PROMOTION_THREAD
    with _PROMOTION_LOCK:
        if _PROMOTION_THREAD is not None and _PROMOTION_THREAD.is_alive():
            return
        interval = int(
            secure_server.runtime_orchestrator.settings().get("idleSleepSeconds") or 1
        )

        def monitor() -> None:
            while not _shutdown_started(runtime_core):
                if _promote_from_standby_once(
                    runtime_core,
                    symbol_worker,
                    setup_worker,
                    execution_handoff,
                ):
                    return
                time.sleep(interval)

        _PROMOTION_THREAD = threading.Thread(
            target=monitor,
            name="runtime-leader-promotion",
            daemon=True,
        )
        _PROMOTION_THREAD.start()


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
            _start_standby_promotion(
                runtime_core,
                symbol_worker,
                setup_worker,
                execution_handoff,
            )
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
    """Canonical handler with health and runtime leadership diagnostics."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in {"/healthz", "/api/health"}:
            core.json_response(self, 200, deployment_readiness.liveness_payload())
            return
        if path == "/readyz":
            readiness = deployment_readiness.runtime_readiness(
                core,
                runtime_instance_guard,
                secure_server.runtime_orchestrator,
            )
            core.json_response(self, 200 if readiness.get("ok") else 503, readiness)
            return
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
    startup_environment = deployment_readiness.require_environment()
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
    print(f"Startup environment: {startup_environment}", flush=True)
    print(f"Runtime leadership: {runtime_instance_guard.snapshot()}", flush=True)
    print(f"Durable state: {core.durable_state_status()}", flush=True)
    print(f"Worker runtime: {secure_server.runtime_orchestrator.snapshot()}", flush=True)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        lifecycle.finalize()


if __name__ == "__main__":
    run()
