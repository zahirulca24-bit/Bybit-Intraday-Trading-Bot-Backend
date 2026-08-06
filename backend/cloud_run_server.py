"""Cloud Run-safe entrypoint for the canonical Bybit Demo backend.

This wrapper preserves the existing secure runtime while adding:
- validated fail-fast Cloud Run configuration;
- public liveness and readiness probes with no secret values;
- PostgreSQL advisory-lock single-leader ownership for automatic workers;
- fail-closed checks on order-capable mutation routes;
- periodic leader-connection verification inside the worker scheduler;
- automatic recovery after a retained leader session is lost;
- standby-to-leader promotion after deployment overlap ends;
- shutdown-safe, exactly-once worker promotion;
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

_BACKEND_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPOSITORY_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

if __package__:
    from . import (
        deployment_readiness,
        historical_execution_backfill,
        runtime_instance_guard,
        runtime_lifecycle,
        secure_server,
    )
else:
    import deployment_readiness
    import historical_execution_backfill
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
_PROMOTION_CONTEXT: tuple[Any, Any, Any, Any | None] | None = None
_WORKERS_STARTED = False
_RECOVERY_STATE: dict[str, Any] = {
    "status": "idle",
    "attempts": 0,
    "startedAt": 0,
    "lastAttemptAt": 0,
    "recoveredAt": 0,
    "lastError": None,
}


def _leadership_reason() -> str:
    status = runtime_instance_guard.snapshot()
    return str(status.get("reason") or "This instance is not the automatic-execution leader.")


def _disable_execution(reason: str, runtime_core: Any | None = None) -> None:
    target_core = runtime_core or core
    with target_core.BOT_LOCK:
        target_core.BOT_STATE.update({
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


def _remember_promotion_context(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None,
) -> None:
    global _PROMOTION_CONTEXT
    _PROMOTION_CONTEXT = (
        runtime_core,
        symbol_worker,
        setup_worker,
        execution_handoff,
    )


def _recovery_snapshot() -> dict[str, Any]:
    with _PROMOTION_LOCK:
        payload = dict(_RECOVERY_STATE)
        payload["threadAlive"] = bool(
            _PROMOTION_THREAD is not None and _PROMOTION_THREAD.is_alive()
        )
        return payload


def _start_workers_once(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> bool:
    """Start the worker orchestrator once, or release leadership on failure."""
    global _WORKERS_STARTED
    with _PROMOTION_LOCK:
        if _WORKERS_STARTED:
            return True
        if _shutdown_started(runtime_core):
            reason = "Runtime shutdown started before worker promotion completed."
            runtime_instance_guard.release(runtime_core, reason)
            _disable_execution(reason, runtime_core)
            return False
        try:
            _ORIGINAL_ORCHESTRATOR_START(
                runtime_core,
                symbol_worker,
                setup_worker,
                execution_handoff,
            )
        except Exception as exc:
            reason = f"Worker orchestrator failed to start after leadership acquisition: {exc}"
            runtime_instance_guard.release(runtime_core, reason)
            _disable_execution(reason, runtime_core)
            return False
        _WORKERS_STARTED = True
        return True


def _promote_from_standby_once(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> bool:
    """Reacquire leadership and resume or start workers exactly once."""
    with _PROMOTION_LOCK:
        if _shutdown_started(runtime_core):
            _disable_execution(
                "Runtime shutdown is in progress; promotion is blocked.",
                runtime_core,
            )
            return False

        # Always reacquire before considering workers already started. A live
        # orchestrator thread can remain present after its retained PostgreSQL
        # advisory-lock connection is lost, but it must not resume execution
        # until a new session owns the lock.
        leadership = runtime_instance_guard.acquire(runtime_core)
        if not leadership.get("leader"):
            reason = str(leadership.get("reason") or "Runtime leadership denied.")
            _RECOVERY_STATE["lastError"] = reason
            _disable_execution(reason, runtime_core)
            return False

        if _shutdown_started(runtime_core):
            reason = "Runtime shutdown started after leadership acquisition; promotion was cancelled."
            runtime_instance_guard.release(runtime_core, reason)
            _disable_execution(reason, runtime_core)
            return False

        # If the original worker loop is still alive, acquiring the new lock is
        # sufficient. The next scheduler cycle can safely continue without
        # starting a duplicate worker thread.
        if _WORKERS_STARTED:
            return True

        return _start_workers_once(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
        )


def _start_standby_promotion(
    runtime_core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> None:
    """Recover leadership with bounded exponential backoff."""
    global _PROMOTION_THREAD
    _remember_promotion_context(
        runtime_core,
        symbol_worker,
        setup_worker,
        execution_handoff,
    )
    with _PROMOTION_LOCK:
        if _PROMOTION_THREAD is not None and _PROMOTION_THREAD.is_alive():
            return

        _base_default = secure_server.runtime_orchestrator.settings().get("idleSleepSeconds") or 1
        try:
            base_delay = max(
                1.0,
                float(os.environ.get("RUNTIME_LEADER_RECOVERY_BASE_SECONDS", _base_default)),
            )
        except (ValueError, TypeError):
            base_delay = max(1.0, float(_base_default))
        try:
            max_delay = max(
                base_delay,
                float(os.environ.get("RUNTIME_LEADER_RECOVERY_MAX_SECONDS", "30")),
            )
        except (ValueError, TypeError):
            max_delay = max(base_delay, 30.0)
        _RECOVERY_STATE.update({
            "status": "recovering",
            "attempts": 0,
            "startedAt": int(time.time()),
            "lastAttemptAt": 0,
            "recoveredAt": 0,
            "lastError": None,
        })

        def monitor() -> None:
            delay = base_delay
            while not _shutdown_started(runtime_core):
                _RECOVERY_STATE["attempts"] = int(_RECOVERY_STATE.get("attempts") or 0) + 1
                _RECOVERY_STATE["lastAttemptAt"] = int(time.time())
                if _promote_from_standby_once(
                    runtime_core,
                    symbol_worker,
                    setup_worker,
                    execution_handoff,
                ):
                    _RECOVERY_STATE.update({
                        "status": "recovered",
                        "recoveredAt": int(time.time()),
                        "lastError": None,
                    })
                    return
                time.sleep(delay)
                delay = min(max_delay, delay * 2)
            _RECOVERY_STATE["status"] = "stopped"

        _PROMOTION_THREAD = threading.Thread(
            target=monitor,
            name="runtime-leader-promotion",
            daemon=True,
        )
        _PROMOTION_THREAD.start()


def _start_recovery_from_saved_context() -> None:
    context = _PROMOTION_CONTEXT
    if context is None:
        return
    _start_standby_promotion(*context)


def _install_orchestrator_guard() -> None:
    """Acquire leadership only after durable PostgreSQL state is installed."""

    def guarded_start(
        runtime_core: Any,
        symbol_worker: Any,
        setup_worker: Any,
        execution_handoff: Any | None = None,
    ) -> dict[str, Any]:
        _remember_promotion_context(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
        )
        leadership = runtime_instance_guard.install(runtime_core)
        if not leadership.get("leader"):
            _disable_execution(
                str(leadership.get("reason") or "Runtime leadership denied."),
                runtime_core,
            )
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
                "runtimeLeadershipRecovery": _recovery_snapshot(),
            }
        started = _start_workers_once(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
        )
        if not started:
            return {
                "status": "blocked",
                "threadAlive": False,
                "lastError": _leadership_reason(),
                "runtimeLeadership": runtime_instance_guard.snapshot(),
                "runtimeLeadershipRecovery": _recovery_snapshot(),
            }
        return secure_server.runtime_orchestrator.snapshot()

    def guarded_run_due_once(
        runtime_core: Any,
        symbol_worker: Any,
        setup_worker: Any,
        execution_handoff: Any | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        _remember_promotion_context(
            runtime_core,
            symbol_worker,
            setup_worker,
            execution_handoff,
        )
        if not runtime_instance_guard.is_leader():
            reason = _leadership_reason()
            _disable_execution(reason, runtime_core)
            _start_standby_promotion(
                runtime_core,
                symbol_worker,
                setup_worker,
                execution_handoff,
            )
            return {
                "status": "standby",
                "threadAlive": False,
                "lastError": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
                "runtimeLeadershipRecovery": _recovery_snapshot(),
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
            _disable_execution(reason, core)
            _start_recovery_from_saved_context()
            core.json_response(instance, 503, {
                "ok": False,
                "enabled": False,
                "reason": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
                "runtimeLeadershipRecovery": _recovery_snapshot(),
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
                core, runtime_instance_guard, secure_server.runtime_orchestrator
            )
            readiness["runtimeLeadershipRecovery"] = _recovery_snapshot()
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
                "runtimeLeadershipRecovery": _recovery_snapshot(),
                "workerRuntime": secure_server.runtime_orchestrator.snapshot(),
            })
            return
        if path == "/api/live-executions/backfill/status":
            if secure_server.reject_disallowed_origin(self):
                return
            if not secure_server.authorize_get(self, path):
                return
            core.json_response(self, 200, historical_execution_backfill.status())
            return
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == historical_execution_backfill.BACKFILL_PATH:
            if secure_server.reject_disallowed_origin(self):
                return
            if not self.is_authorized():
                core.json_response(self, 401, {"ok": False, "error": "Unauthorized"})
                return
            try:
                payload = core.read_json(self)
            except Exception as exc:
                core.json_response(self, 400, {"ok": False, "error": f"Invalid JSON: {exc}"})
                return
            historical_execution_backfill.handle_post(self, core, path, payload)
            return
        if path in _EXECUTION_MUTATION_PATHS and not runtime_instance_guard.is_leader():
            if secure_server.reject_disallowed_origin(self):
                return
            if not self.is_authorized():
                core.json_response(self, 401, {"ok": False, "error": "Unauthorized"})
                return
            reason = _leadership_reason()
            _disable_execution(reason, core)
            _start_recovery_from_saved_context()
            core.json_response(self, 503, {
                "ok": False,
                "enabled": False,
                "reason": reason,
                "runtimeLeadership": runtime_instance_guard.snapshot(),
                "runtimeLeadershipRecovery": _recovery_snapshot(),
            })
            return
        return super().do_POST()


def run() -> None:
    startup_environment = deployment_readiness.require_environment()
    install_cloud_run_safety()
    secure_server.install_secure_runtime()
    _install_start_gate()

    try:
        backfill_days = int(os.environ.get("EXECUTION_LEDGER_HISTORICAL_BACKFILL_DAYS", "30"))
    except ValueError:
        backfill_days = 30
    historical_execution_backfill.start_once(core, days=backfill_days)

    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), CloudRunSecureHandler)
    lifecycle = runtime_lifecycle.RuntimeLifecycle(
        core, server, secure_server.runtime_orchestrator, runtime_instance_guard
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
