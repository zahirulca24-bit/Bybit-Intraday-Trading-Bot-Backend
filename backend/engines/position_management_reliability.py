"""Fail-closed reliability latch for position-management retry cooldowns.

A failed protection action is intentionally suppressed during its retry cooldown.
The base manager reports that suppressed action as ``skipped``.  Without this
latch, a second management pass can overwrite ``tradeManagement=error`` with
``ok`` even though the exchange never confirmed the protection update.
"""

from __future__ import annotations

from typing import Any, Callable

_COOLDOWN_REASON = "Previous failure is inside the retry cooldown."


def _cooldown_failure_is_latched(result: dict[str, Any]) -> bool:
    actions = result.get("actions") or []
    return any(
        isinstance(action, dict)
        and action.get("status") == "skipped"
        and str(action.get("reason") or "") == _COOLDOWN_REASON
        for action in actions
    )


def install(position_management_module: Any) -> None:
    """Wrap ``manage_positions`` once and preserve unresolved failure status."""
    if getattr(position_management_module, "_reliability_latch_installed", False):
        return

    original: Callable[..., Any] = position_management_module.manage_positions

    def reliable_manage_positions(core: Any, state: dict[str, Any], *args: Any, **kwargs: Any):
        result = original(core, state, *args, **kwargs)
        if not isinstance(result, dict) or not _cooldown_failure_is_latched(result):
            return result

        result = dict(result)
        try:
            failures = int(result.get("failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        result["ok"] = False
        result["failures"] = max(1, failures)
        result["reason"] = (
            "Position management remains degraded while a failed protection "
            "action is inside the retry cooldown."
        )

        try:
            engine = core.get_bot_engine()
        except Exception:
            engine = None
        if engine is not None and hasattr(engine, "set_status"):
            engine.set_status("tradeManagement", "error")
        return result

    position_management_module.manage_positions = reliable_manage_positions
    position_management_module._reliability_latch_installed = True
