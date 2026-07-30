"""Graceful process lifecycle for Cloud Run and other container runtimes."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any


class RuntimeLifecycle:
    def __init__(self, core: Any, server: Any, orchestrator: Any, instance_guard: Any):
        self.core = core
        self.server = server
        self.orchestrator = orchestrator
        self.instance_guard = instance_guard
        self._lock = threading.RLock()
        self._shutdown_started = False
        self._shutdown_complete = False
        self._cleanup_done = False
        self._reason = "Runtime is active."
        self._requested_at = 0
        self._completed_at = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "shutdownStarted": self._shutdown_started,
                "shutdownComplete": self._shutdown_complete,
                "reason": self._reason,
                "requestedAt": self._requested_at,
                "completedAt": self._completed_at,
            }

    def _update_core(self, reason: str, status: str) -> None:
        lock = getattr(self.core, "BOT_LOCK", None)
        state = getattr(self.core, "BOT_STATE", None)
        if lock is None or not isinstance(state, dict):
            return
        with lock:
            state["enabled"] = False
            state["runtimeLifecycle"] = {
                "status": status,
                "reason": reason,
                "requestedAt": self._requested_at,
                "completedAt": self._completed_at,
            }
            state["lastReason"] = reason

    def request_shutdown(self, reason: str) -> bool:
        """Start shutdown from a helper thread so HTTPServer.shutdown cannot deadlock."""
        with self._lock:
            if self._shutdown_started:
                return False
            self._shutdown_started = True
            self._reason = str(reason)
            self._requested_at = int(time.time())
            self._update_core(self._reason, "stopping")

        threading.Thread(
            target=self._shutdown_worker,
            name="runtime-graceful-shutdown",
            daemon=True,
        ).start()
        return True

    def _shutdown_worker(self) -> None:
        try:
            self.orchestrator.stop(timeout=5.0)
        finally:
            self.instance_guard.release(self.core, self._reason)
            with self._lock:
                self._cleanup_done = True
            self.server.shutdown()

    def finalize(self) -> dict[str, Any]:
        """Idempotently close workers, leadership, and the listening socket."""
        with self._lock:
            if self._shutdown_complete:
                return self.snapshot()
            if not self._shutdown_started:
                self._shutdown_started = True
                self._reason = "Runtime server stopped."
                self._requested_at = int(time.time())
                self._update_core(self._reason, "stopping")
            cleanup_needed = not self._cleanup_done

        if cleanup_needed:
            self.orchestrator.stop(timeout=5.0)
            self.instance_guard.release(self.core, self._reason)
            with self._lock:
                self._cleanup_done = True
        self.server.server_close()

        with self._lock:
            self._shutdown_complete = True
            self._completed_at = int(time.time())
            self._update_core(self._reason, "stopped")
            return self.snapshot()

    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            try:
                signal_name = signal.Signals(signum).name
            except Exception:
                signal_name = str(signum)
            self.request_shutdown(f"Received {signal_name}; graceful shutdown requested.")

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
