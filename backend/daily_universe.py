"""Disabled legacy Daily/1D universe stage.

The active scanner now starts from direct eligible USDT contracts and the
closed-1H Top-20 watchlist. This compatibility module intentionally performs no
runtime filtering and exists only so older imports/status readers do not fail.
"""

from __future__ import annotations

from typing import Any

_DISABLED = {
    "status": "disabled",
    "version": 2,
    "source": "removed_from_active_scan_pipeline",
    "symbols": [],
    "rows": [],
    "metrics": {
        "active": False,
        "reason": "Daily/1D gate removed; canonical pipeline starts at closed 1H",
    },
    "lastError": None,
    "persisted": False,
}


def settings() -> dict[str, Any]:
    return {"enabled": False}


def snapshot() -> dict[str, Any]:
    return {**_DISABLED, "settings": settings()}


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    return snapshot()


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    return snapshot()


def due(now: int | None = None) -> bool:
    return False


def install(core: Any, worker_module: Any) -> dict[str, Any]:
    core.daily_master_universe = lambda force=False: snapshot()
    core.daily_master_universe_status = snapshot
    return status(worker_module)


def status(worker_module: Any | None = None) -> dict[str, Any]:
    return {
        "installed": False,
        "enabled": False,
        "policy": "REMOVED_FROM_ACTIVE_SCAN_PIPELINE",
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    return None
