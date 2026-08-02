"""Automatic scheduler for staged symbol selection and entry preparation."""

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
    "lastEntryRunAt": 0,
    "lastRiskRunAt": 0,
    "lastSizingRunAt": 0,
    "lastExecutionRunAt": 0,
    "nextSymbolRunAt": 0,
    "nextSetupRunAt": 0,
    "nextExecutionRunAt": 0,
    "symbolRuns": 0,
    "setupRuns": 0,
    "entryRuns": 0,
    "riskRuns": 0,
    "sizingRuns": 0,
    "executionRuns": 0,
    "legacySetupWorkerDisabled": True,
    "legacyPythonExecutionDisabled": True,
    "lastError": None,
}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, Any]:
    return {
        "symbolIntervalSeconds": _integer(
            "SYMBOL_WORKER_INTERVAL_SECONDS", 300, 30, 3600
        ),
        "setupIntervalSeconds": _integer(
            "SETUP_WORKER_INTERVAL_SECONDS", 300, 30, 3600
        ),
        "legacyExecutionIntervalSeconds": _integer(
            "EXECUTION_HANDOFF_INTERVAL_SECONDS", 30, 5, 300
        ),
        "idleSleepSeconds": _integer(
            "WORKER_ORCHESTRATOR_IDLE_SECONDS", 1, 1, 10
        ),
        "legacySetupWorkerEnabled": False,
        "legacyPythonExecutionEnabled": False,
        "entryConfirmationCadence": "NEW_CLOSED_5M_ON_SETUP_CYCLE",
        "riskDecisionCadence": "AFTER_CLOSED_5M_ENTRY_CONFIRMATION",
        "positionSizingCadence": "AFTER_AUTHORITATIVE_RISK_APPROVAL",
    }


def _run_fifteen_minute_strategy_classifier(
    core: Any, now: int
) -> dict[str, Any]:
    try:
        from . import fifteen_minute_strategy_classifier
    except ImportError:
        import fifteen_minute_strategy_classifier
    return fifteen_minute_strategy_classifier.ensure_current(core, now=now)


def _run_five_minute_entry_confirmation(
    core: Any, now: int
) -> dict[str, Any]:
    try:
        from . import five_minute_entry_confirmation
    except ImportError:
        import five_minute_entry_confirmation
    return five_minute_entry_confirmation.ensure_current(core, now=now)


def _run_authoritative_entry_risk(
    core: Any, now: int
) -> dict[str, Any]:
    try:
        from . import authoritative_entry_risk
    except ImportError:
        import authoritative_entry_risk
    return authoritative_entry_risk.ensure_current(core, now=now)


def _run_position_sizing_margin(
    core: Any, now: int
) -> dict[str, Any]:
    try:
        from . import position_sizing_margin
    except ImportError:
        import position_sizing_margin
    return position_sizing_margin.ensure_current(core, now=now)


def run_due_once(
    core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Run staged scanner components; legacy Python entry execution stays off."""
    timestamp = int(now or time.time())
    cfg = settings()
    with _LOCK:
        symbol_due = int(_STATE.get("nextSymbolRunAt") or 0) <= timestamp
        setup_due = int(_STATE.get("nextSetupRunAt") or 0) <= timestamp

    try:
        if symbol_due:
            symbol_worker.run_batch(core, now=timestamp)
            with _LOCK:
                _STATE["lastSymbolRunAt"] = timestamp
                _STATE["nextSymbolRunAt"] = (
                    timestamp + int(cfg["symbolIntervalSeconds"])
                )
                _STATE["symbolRuns"] = int(_STATE.get("symbolRuns") or 0) + 1

        if setup_due:
            _run_fifteen_minute_strategy_classifier(core, timestamp)
            _run_five_minute_entry_confirmation(core, timestamp)
            _run_authoritative_entry_risk(core, timestamp)
            _run_position_sizing_margin(core, timestamp)
            with _LOCK:
                _STATE["lastSetupRunAt"] = timestamp
                _STATE["lastEntryRunAt"] = timestamp
                _STATE["lastRiskRunAt"] = timestamp
                _STATE["lastSizingRunAt"] = timestamp
                _STATE["nextSetupRunAt"] = (
                    timestamp + int(cfg["setupIntervalSeconds"])
                )
                _STATE["setupRuns"] = int(_STATE.get("setupRuns") or 0) + 1
                _STATE["entryRuns"] = int(_STATE.get("entryRuns") or 0) + 1
                _STATE["riskRuns"] = int(_STATE.get("riskRuns") or 0) + 1
                _STATE["sizingRuns"] = int(_STATE.get("sizingRuns") or 0) + 1

        # Deliberate cutover: do not call setup_worker.run_batch() and do not
        # call execution_handoff.run_once(). The modules remain available for
        # audit/rollback, but new entries wait for Node.js execution.
        with _LOCK:
            _STATE["legacySetupWorkerDisabled"] = True
            _STATE["legacyPythonExecutionDisabled"] = True
            _STATE["nextExecutionRunAt"] = 0
            _STATE["lastLoopAt"] = timestamp
            _STATE["lastError"] = None
        return snapshot()
    except Exception as exc:
        with _LOCK:
            _STATE["status"] = "error"
            _STATE["lastLoopAt"] = timestamp
            _STATE["lastError"] = str(exc)
        return snapshot()


def _loop(
    core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None,
) -> None:
    with _LOCK:
        _STATE["status"] = "running"
    while not _STOP_EVENT.is_set():
        run_due_once(core, symbol_worker, setup_worker, execution_handoff)
        _STOP_EVENT.wait(settings()["idleSleepSeconds"])
    with _LOCK:
        _STATE["status"] = "stopped"
        _STATE["stoppedAt"] = int(time.time())


def _install_daily_universe(core: Any, symbol_worker: Any) -> None:
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


def _install_four_hour_directional_pool(
    core: Any, symbol_worker: Any
) -> None:
    try:
        from . import four_hour_directional_pool
    except ImportError:
        import four_hour_directional_pool
    four_hour_directional_pool.install(core, symbol_worker)


def _four_hour_directional_pool_status() -> dict[str, Any]:
    try:
        from . import four_hour_directional_pool
    except ImportError:
        try:
            import four_hour_directional_pool
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return four_hour_directional_pool.snapshot()


def _install_hourly_watchlist(core: Any, symbol_worker: Any) -> None:
    try:
        from . import hourly_watchlist
    except ImportError:
        import hourly_watchlist
    hourly_watchlist.install(core, symbol_worker)


def _hourly_watchlist_status() -> dict[str, Any]:
    try:
        from . import hourly_watchlist
    except ImportError:
        try:
            import hourly_watchlist
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return hourly_watchlist.snapshot()


def _install_fifteen_minute_strategy_classifier(
    core: Any, setup_worker: Any
) -> None:
    try:
        from . import fifteen_minute_strategy_classifier
    except ImportError:
        import fifteen_minute_strategy_classifier
    fifteen_minute_strategy_classifier.install(core, setup_worker)


def _fifteen_minute_strategy_classifier_status() -> dict[str, Any]:
    try:
        from . import fifteen_minute_strategy_classifier
    except ImportError:
        try:
            import fifteen_minute_strategy_classifier
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return fifteen_minute_strategy_classifier.snapshot()


def _install_five_minute_entry_confirmation(
    core: Any, setup_worker: Any
) -> None:
    try:
        from . import five_minute_entry_confirmation
    except ImportError:
        import five_minute_entry_confirmation
    five_minute_entry_confirmation.install(core, setup_worker)


def _five_minute_entry_confirmation_status() -> dict[str, Any]:
    try:
        from . import five_minute_entry_confirmation
    except ImportError:
        try:
            import five_minute_entry_confirmation
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return five_minute_entry_confirmation.snapshot()


def _install_authoritative_entry_risk(core: Any) -> None:
    try:
        from . import authoritative_entry_risk
    except ImportError:
        import authoritative_entry_risk
    authoritative_entry_risk.install(core)


def _authoritative_entry_risk_status() -> dict[str, Any]:
    try:
        from . import authoritative_entry_risk
    except ImportError:
        try:
            import authoritative_entry_risk
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return authoritative_entry_risk.snapshot()


def _install_position_sizing_margin(core: Any, setup_worker: Any) -> None:
    try:
        from . import position_sizing_margin
    except ImportError:
        import position_sizing_margin
    position_sizing_margin.install(core, setup_worker)


def _position_sizing_margin_status() -> dict[str, Any]:
    try:
        from . import position_sizing_margin
    except ImportError:
        try:
            import position_sizing_margin
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return position_sizing_margin.snapshot()


def _install_issue1_policy(core: Any) -> None:
    try:
        from . import issue1_risk_exit_policy
        from . import position_synced_server as verified
    except ImportError:
        import issue1_risk_exit_policy
        import position_synced_server as verified
    issue1_risk_exit_policy.install(core, verified)


def _install_strategy_step1(core: Any) -> None:
    try:
        from . import strategy_step1_upgrade
    except ImportError:
        import strategy_step1_upgrade
    strategy_step1_upgrade.install(core)


def _install_strategy_step2(core: Any, setup_worker: Any) -> None:
    try:
        from . import strategy_step2_upgrade
    except ImportError:
        import strategy_step2_upgrade
    strategy_step2_upgrade.install(core, setup_worker)


def _install_strategy_step3(core: Any) -> None:
    try:
        from . import analytics_runtime, strategy_step3_upgrade
    except ImportError:
        import analytics_runtime
        import strategy_step3_upgrade
    strategy_step3_upgrade.install(core, analytics_runtime)


def _install_python_execution_cutover(core: Any) -> None:
    try:
        from . import python_execution_cutover
    except ImportError:
        import python_execution_cutover
    python_execution_cutover.install(core)


def _python_execution_cutover_status(core: Any | None = None) -> dict[str, Any]:
    try:
        from . import python_execution_cutover
    except ImportError:
        try:
            import python_execution_cutover
        except ImportError:
            return {"installed": False, "status": "unavailable"}
    return python_execution_cutover.status(core)


def start(
    core: Any,
    symbol_worker: Any,
    setup_worker: Any,
    execution_handoff: Any | None = None,
) -> dict[str, Any]:
    """Start one scanner scheduler with Python automatic entries disabled."""
    global _THREAD
    _install_daily_universe(core, symbol_worker)
    _install_four_hour_directional_pool(core, symbol_worker)
    _install_hourly_watchlist(core, symbol_worker)
    _install_issue1_policy(core)
    _install_strategy_step1(core)
    _install_strategy_step2(core, setup_worker)
    _install_strategy_step3(core)
    _install_fifteen_minute_strategy_classifier(core, setup_worker)
    _install_five_minute_entry_confirmation(core, setup_worker)
    _install_authoritative_entry_risk(core)
    _install_position_sizing_margin(core, setup_worker)
    _install_python_execution_cutover(core)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return snapshot_unlocked(core)
        now = int(time.time())
        _STOP_EVENT.clear()
        _STATE.update(
            {
                "status": "starting",
                "startedAt": now,
                "stoppedAt": 0,
                "nextSymbolRunAt": now,
                "nextSetupRunAt": now,
                "nextExecutionRunAt": 0,
                "legacySetupWorkerDisabled": True,
                "legacyPythonExecutionDisabled": True,
                "lastError": None,
            }
        )
        _THREAD = threading.Thread(
            target=_loop,
            args=(core, symbol_worker, setup_worker, execution_handoff),
            name="worker-runtime-orchestrator",
            daemon=True,
        )
        _THREAD.start()
        return snapshot_unlocked(core)


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


def snapshot_unlocked(core: Any | None = None) -> dict[str, Any]:
    return {
        **dict(_STATE),
        "threadAlive": bool(_THREAD is not None and _THREAD.is_alive()),
        "settings": settings(),
        "dailyUniverse": _daily_universe_status(),
        "fourHourDirectionalPool": _four_hour_directional_pool_status(),
        "hourlyWatchlist": _hourly_watchlist_status(),
        "fifteenMinuteStrategyClassification": (
            _fifteen_minute_strategy_classifier_status()
        ),
        "fiveMinuteEntryConfirmation": (
            _five_minute_entry_confirmation_status()
        ),
        "authoritativeEntryRisk": _authoritative_entry_risk_status(),
        "positionSizingMargin": _position_sizing_margin_status(),
        "pythonExecutionCutover": _python_execution_cutover_status(core),
    }


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return snapshot_unlocked()
