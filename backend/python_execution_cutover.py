"""Fail-closed cutover guard for the future Node.js execution authority.

The existing Python execution code remains intact for auditability and rollback,
but automatic entry submission is blocked after the closed-5M confirmation
pipeline is installed. Manual demo connection tests and open-position management
are not replaced by this module.
"""

from __future__ import annotations

from typing import Any


_INSTALLED_ATTR = "_python_auto_execution_cutover_v1_installed"
_BLOCK_CODE = "PYTHON_AUTO_EXECUTION_DISABLED"
_BLOCK_RET_CODE = -2606
_AUTO_SOURCES = {"auto", "setup-worker"}


def _blocked_result(source: str, symbol: str, side: str) -> dict[str, Any]:
    return {
        "retCode": _BLOCK_RET_CODE,
        "retMsg": (
            "Automatic Python order execution is disabled; confirmed entries "
            "must wait for the approved Node.js execution service."
        ),
        "result": {},
        "ok": False,
        "code": _BLOCK_CODE,
        "source": source,
        "symbol": symbol,
        "side": side,
        "orderSubmitted": False,
        "executionAuthority": "NODE_JS_PENDING",
    }


def install(core: Any) -> dict[str, Any]:
    if getattr(core, _INSTALLED_ATTR, False):
        return status(core)

    original_place_demo_order = core.place_demo_order
    engine = core.get_bot_engine()
    original_engine_execute = engine.execute

    def guarded_place_demo_order(
        symbol: Any,
        side: Any,
        qty: Any,
        source: Any,
        stop_loss_pct: Any = None,
        take_profit_pct: Any = None,
    ) -> dict[str, Any]:
        normalized_source = str(source or "").strip().lower()
        if normalized_source in _AUTO_SOURCES:
            return _blocked_result(
                normalized_source,
                str(symbol or "").upper(),
                str(side or ""),
            )
        return original_place_demo_order(
            symbol,
            side,
            qty,
            source,
            stop_loss_pct,
            take_profit_pct,
        )

    def blocked_engine_execute(state: dict[str, Any], signal: str) -> dict[str, Any]:
        symbol = str((state or {}).get("symbol") or "").upper()
        result = _blocked_result("auto", symbol, str(signal or ""))
        engine.set_status("tradeManagement", "blocked")
        try:
            engine.journal.add(
                "python_auto_execution_blocked",
                {
                    "symbol": symbol,
                    "signal": signal,
                    "result": result,
                },
            )
            engine.set_status("journal", "ok")
        except Exception:
            pass
        return result

    core._python_execution_cutover_original_place_demo_order = original_place_demo_order
    core._python_execution_cutover_original_engine_execute = original_engine_execute
    core.place_demo_order = guarded_place_demo_order
    engine.execute = blocked_engine_execute
    setattr(core, _INSTALLED_ATTR, True)

    try:
        with core.BOT_LOCK:
            core.BOT_STATE.update(
                {
                    "executionAuthority": "NODE_JS_PENDING",
                    "legacyPythonAutoExecutionDisabled": True,
                    "lastReason": (
                        "Scanner pipeline active; automatic Python entries are "
                        "disabled pending Node.js execution authority."
                    ),
                }
            )
    except Exception:
        pass
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    installed = bool(
        core is not None and getattr(core, _INSTALLED_ATTR, False)
    )
    return {
        "installed": installed,
        "policy": "FAIL_CLOSED_PYTHON_AUTOMATIC_ENTRY",
        "blockedSources": sorted(_AUTO_SOURCES),
        "manualDemoOrderPreserved": True,
        "openPositionManagementPreserved": True,
        "executionAuthority": "NODE_JS_PENDING" if installed else "LEGACY",
        "blockCode": _BLOCK_CODE,
    }
