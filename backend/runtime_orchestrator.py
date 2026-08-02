"""Automatic scheduler for symbol selection, setup verification, and execution handoff."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "status": "stopped",
    "startedAt": 0,
    "stoppedAt": 0,
    "lastLoopAt": 0,
    "lastSymbolRunAt": 0,
    "lastSetupRunAt": 0,
    "lastExecutionRunAt": 0,
    "nextSymbolRunAt": 0,
    "nextSetupRunAt": 0,
    "nextExecutionRunAt": 0,
    "symbolRuns": 0,
    "setupRuns": 0,
    "executionRuns": 0,
    "lastError": None,
}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, int]:
    return {
        "symbolIntervalSeconds": _integer("SYMBOL_WORKER_INTERVAL_SECONDS", 300, 30, 3600),
        "setupIntervalSeconds": _integer("SETUP_WORKER_INTERVAL_SECONDS", 300, 30, 3600),
        "executionIntervalSeconds": _integer("EXECUTION_HANDOFF_INTERVAL_SECONDS", 30, 5, 300),
        "idleSleepSeconds": _integer("WORKER_ORCHESTRATOR_IDLE_SECONDS", 1, 1, 10),
    }


def run_due_once(
    core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Run due components once; guarded execution remains dependent on BOT_STATE.enabled."""
    timestamp = int(now or time.time())
    cfg = settings()
    with _LOCK:
        symbol_due = int(_STATE.get("nextSymbolRunAt") or 0) <= timestamp
        setup_due = int(_STATE.get("nextSetupRunAt") or 0) <= timestamp
        execution_due = execution_handoff is not None and int(_STATE.get("nextExecutionRunAt") or 0) <= timestamp

    try:
        if symbol_due:
            symbol_worker.run_batch(core, now=timestamp)
            with _LOCK:
                _STATE["lastSymbolRunAt"] = timestamp
                _STATE["nextSymbolRunAt"] = timestamp + int(cfg["symbolIntervalSeconds"])
                _STATE["symbolRuns"] = int(_STATE.get("symbolRuns") or 0) + 1

        if setup_due:
            setup_worker.run_batch(core, symbol_worker, now=timestamp)
            with _LOCK:
                _STATE["lastSetupRunAt"] = timestamp
                _STATE["nextSetupRunAt"] = timestamp + int(cfg["setupIntervalSeconds"])
                _STATE["setupRuns"] = int(_STATE.get("setupRuns") or 0) + 1

        if execution_due:
            execution_handoff.run_once(core, setup_worker, now=timestamp)
            with _LOCK:
                _STATE["lastExecutionRunAt"] = timestamp
                _STATE["nextExecutionRunAt"] = timestamp + int(cfg["executionIntervalSeconds"])
                _STATE["executionRuns"] = int(_STATE.get("executionRuns") or 0) + 1

        with _LOCK:
            _STATE["lastLoopAt"] = timestamp
            _STATE["lastError"] = None
        return snapshot()
    except Exception as exc:
        with _LOCK:
            _STATE["status"] = "error"
            _STATE["lastLoopAt"] = timestamp
            _STATE["lastError"] = str(exc)
        return snapshot()


def _loop(core: Any, symbol_worker: Any, setup_worker: Any, execution_handoff: Any | None) -> None:
    with _LOCK:
        _STATE["status"] = "running"
    while not _STOP_EVENT.is_set():
        run_due_once(core, symbol_worker, setup_worker, execution_handoff)
        _STOP_EVENT.wait(settings()["idleSleepSeconds"])
    with _LOCK:
        _STATE["status"] = "stopped"
        _STATE["stoppedAt"] = int(time.time())


def _install_daily_universe(core: Any, symbol_worker: Any) -> None:
    """Install the persistent daily Top-100 source after durable state exists."""
    try:
        from . import daily_universe
    except ImportError:
        import daily_universe
    daily_universe.install(core, symbol_worker)


def _daily_universe_status() -> dict[str, Any]:
    try:
        from . import daily_universe
    except ImportError:
        try:
            import daily_universe
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return daily_universe.snapshot()


def _install_issue1_policy(core: Any) -> None:
    """Install after durable state exists and before automatic workers start."""
    try:
        from . import issue1_risk_exit_policy
        from . import position_synced_server as verified
    except ImportError:
        import issue1_risk_exit_policy
        import position_synced_server as verified
    issue1_risk_exit_policy.install(core, verified)


def _install_strategy_step1(core: Any) -> None:
    """Install session-aware ORB and 1H/15M/5M confluence before workers start."""
    try:
        from . import strategy_step1_upgrade
    except ImportError:
        import strategy_step1_upgrade
    strategy_step1_upgrade.install(core)


def _install_strategy_step2(core: Any, setup_worker: Any) -> None:
    """Install ATR SL/TP and deterministic grading before workers start."""
    try:
        from . import strategy_step2_upgrade
    except ImportError:
        import strategy_step2_upgrade
    strategy_step2_upgrade.install(core, setup_worker)


def _install_strategy_step3(core: Any) -> None:
    """Install market-regime filtering and trade-quality analytics."""
    try:
        from . import analytics_runtime, strategy_step3_upgrade
    except ImportError:
        import analytics_runtime
        import strategy_step3_upgrade
    strategy_step3_upgrade.install(core, analytics_runtime)


def start(
    core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> dict[str, Any]:
    """Start exactly one daemon scheduler and make all configured stages due."""
    global _THREAD
    _install_daily_universe(core, symbol_worker)
    _install_issue1_policy(core)
    _install_strategy_step1(core)
    _install_strategy_step2(core, setup_worker)
    _install_strategy_step3(core)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return snapshot_unlocked()
        now = int(time.time())
        _STOP_EVENT.clear()
        _STATE.update({
            "status": "starting",
            "startedAt": now,
            "stoppedAt": 0,
            "nextSymbolRunAt": now,
            "nextSetupRunAt": now,
            "nextExecutionRunAt": now,
            "lastError": None,
        })
        _THREAD = threading.Thread(
            target=_loop,
            args=(core, symbol_worker, setup_worker, execution_handoff),
            name="worker-runtime-orchestrator",
            daemon=True,
        )
        _THREAD.start()
        return snapshot_unlocked()


def stop(timeout: float = 5.0) -> dict[str, Any]:
    global _THREAD
    _STOP_EVENT.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    with _LOCK:
        if thread is None or not thread.is_alive():
            _THREAD = None
            _STATE["status"] = "stopped"
            _STATE["stoppedAt"] = int(time.time())
        return snapshot_unlocked()


def snapshot_unlocked() -> dict[str, Any]:
    return {
        **dict(_STATE),
        "threadAlive": bool(_THREAD is not None and _THREAD.is_alive()),
        "settings": settings(),
        "dailyUniverse": _daily_universe_status(),
    }


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return snapshot_unlocked()
