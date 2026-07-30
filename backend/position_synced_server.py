"""Canonical guarded runtime with exchange-verified safety layers."""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer
from typing import Callable

try:
    from . import guarded_server as guarded
    from .engines.entry_protection import place_mandatory_protected_order
    from .engines.order_fill import (
        clear_pending_entry,
        finalize_entry_order,
        get_pending_entry,
        verify_final_fill,
    )
    from .engines.position_management import (
        entry_gate as position_management_entry_gate,
        manage_positions,
    )
    from .position_sync import collect_open_positions
    from .protection_verification import annotate_protection, protection_gate
except ImportError:
    import guarded_server as guarded
    from engines.entry_protection import place_mandatory_protected_order
    from engines.order_fill import (
        clear_pending_entry,
        finalize_entry_order,
        get_pending_entry,
        verify_final_fill,
    )
    from engines.position_management import (
        entry_gate as position_management_entry_gate,
        manage_positions,
    )
    from position_sync import collect_open_positions
    from protection_verification import annotate_protection, protection_gate


_ORIGINAL_EXISTING_POSITION_GUARD = guarded.core.existing_position_guard
_BASE_BOT_TICK: Callable[[], dict] | None = None


def _positive_size(value) -> bool:
    try:
        return abs(float(value or 0)) > 0
    except (TypeError, ValueError):
        return False


def _pending_fill_guard():
    pending = get_pending_entry()
    if not pending:
        return True, "No unresolved entry order.", None

    symbol = str(pending.get("symbol") or "")
    order_result = pending.get("orderResult") or {}
    verification = verify_final_fill(
        symbol,
        order_result,
        guarded.core.bybit_request,
        attempts=1,
        delay_seconds=0,
    )

    if verification.get("accepted"):
        positions_payload = collect_open_positions(guarded.core.bybit_request)
        result = (
            positions_payload.get("result")
            if isinstance(positions_payload, dict)
            else None
        )
        rows = result.get("list") if isinstance(result, dict) else None
        synchronized = (
            positions_payload.get("retCode") == 0
            and isinstance(rows, list)
            and any(
                isinstance(row, dict)
                and str(row.get("symbol") or "") == symbol
                and _positive_size(row.get("size"))
                for row in rows
            )
        )
        if synchronized:
            clear_pending_entry()
            return (
                False,
                "Previous filled entry is now position-synchronized; this cycle remains blocked.",
                verification,
            )
        return (
            False,
            "Previous entry is Filled but its non-zero position is not synchronized yet.",
            verification,
        )

    if verification.get("terminal") and not verification.get("unresolved"):
        clear_pending_entry()
        return True, "Previous entry resolved without a fill.", verification

    return (
        False,
        (
            "Previous entry fill remains unresolved: "
            f"{verification.get('reason', 'status unavailable')}"
        ),
        verification,
    )


def _protected_existing_position_guard(symbol, signal, state):
    management_ok, management_reason, management_verification = (
        position_management_entry_gate(guarded.core)
    )
    if not management_ok:
        return {
            "ok": False,
            "reason": f"New entry blocked: {management_reason}",
            "positions": [],
            "sameDirection": False,
            "oppositeDirection": False,
            "positionManagementBlocked": True,
            "positionManagementVerification": management_verification,
        }

    pending_ok, pending_reason, pending_verification = _pending_fill_guard()
    if not pending_ok:
        return {
            "ok": False,
            "reason": f"New entry blocked: {pending_reason}",
            "positions": [],
            "sameDirection": False,
            "oppositeDirection": False,
            "fillVerificationBlocked": True,
            "fillVerification": pending_verification,
        }

    positions_payload = collect_open_positions(guarded.core.bybit_request)
    protection_ok, reason = protection_gate(positions_payload)
    result = (
        positions_payload.get("result")
        if isinstance(positions_payload, dict)
        else None
    )
    positions = (
        result.get("list")
        if isinstance(result, dict) and isinstance(result.get("list"), list)
        else []
    )

    if not protection_ok:
        return {
            "ok": False,
            "reason": f"New entry blocked: {reason}",
            "positions": positions,
            "sameDirection": False,
            "oppositeDirection": False,
            "protectionBlocked": True,
        }
    return _ORIGINAL_EXISTING_POSITION_GUARD(symbol, signal, state)


def _mandatory_place_demo_order(
    symbol,
    side,
    qty,
    source,
    stop_loss_pct=None,
    take_profit_pct=None,
):
    create_result = place_mandatory_protected_order(
        symbol,
        side,
        qty,
        source,
        stop_loss_pct,
        take_profit_pct,
        get_mark_price=guarded.core.get_mark_price,
        get_instrument_rules=guarded.core.get_instrument_rules,
        generate_order_link_id=guarded.core.generate_order_link_id,
        submit_order=guarded.core.bybit_request,
    )
    return finalize_entry_order(
        symbol,
        create_result,
        guarded.core.bybit_request,
    )


def _verified_manage_open_positions(state):
    gate_ok, gate_reason, gate_verification = position_management_entry_gate(
        guarded.core
    )
    if not gate_ok:
        engine = guarded.core.get_bot_engine()
        engine.set_status("tradeManagement", "blocked")
        return {
            "ok": False,
            "actions": [],
            "failures": 1,
            "skipped": 0,
            "reason": gate_reason,
            "pendingPartialClose": gate_verification,
        }
    return manage_positions(guarded.core, state)


def _fill_aware_bot_tick():
    """Refine the guarded lifecycle after a newly submitted order is finalized."""
    if _BASE_BOT_TICK is None:
        raise RuntimeError("Fill-aware bot tick installed without a guarded base tick")

    with guarded.core.BOT_LOCK:
        previous_order = guarded.core.BOT_STATE.get("lastOrder")

    status = _BASE_BOT_TICK()
    current_order = status.get("lastOrder")
    if current_order is previous_order or not isinstance(current_order, dict):
        return status

    verification = current_order.get("fillVerification")
    if not isinstance(verification, dict):
        return status

    signal = status.get("lastSignal") or "WAIT"
    state = str(verification.get("state") or "unknown")
    reason = str(
        verification.get("reason") or "Final fill verification unavailable"
    )
    accepted = bool(verification.get("accepted"))
    unresolved = bool(verification.get("unresolved"))

    if accepted:
        updates = {
            "lastReason": (
                f"{signal} entry fully filled with qty "
                f"{verification.get('cumExecQty') or status.get('qty') or '-'}"
            ),
            "orderLifecycle": guarded.core.order_lifecycle(
                signal=signal,
                guard="passed",
                order="filled",
                protection="attached",
                status="filled",
                reason=reason,
            ),
        }
    else:
        lifecycle_state = {
            "partial": "partial",
            "cancelled": "cancelled",
            "rejected": "rejected",
            "create_rejected": "rejected",
            "timeout": "timeout",
            "verification_error": "verification_failed",
            "invalid_fill": "verification_failed",
            "invalid_create_response": "verification_failed",
        }.get(state, "unknown")
        updates = {
            "lastReason": f"Order not accepted as filled: {reason}",
            "executionGuard": {
                "ok": False,
                "reason": f"final fill {lifecycle_state}",
            },
            "orderLifecycle": guarded.core.order_lifecycle(
                signal=signal,
                guard="passed",
                order=lifecycle_state,
                protection="blocked" if unresolved else "skipped",
                status=lifecycle_state,
                reason=reason,
            ),
        }
        if unresolved:
            updates["enabled"] = False

    with guarded.core.BOT_LOCK:
        guarded.core.BOT_STATE.update(updates)
        return dict(guarded.core.BOT_STATE)


def install_position_management() -> None:
    """Replace legacy acknowledgement-based management with verified actions."""
    guarded.core.manage_open_positions = _verified_manage_open_positions


def install_mandatory_entry_protection() -> None:
    """Install strict protection, final-fill, and unresolved-entry gates."""
    global _BASE_BOT_TICK
    guarded.core.place_demo_order = _mandatory_place_demo_order
    if guarded.core.bot_tick is not _fill_aware_bot_tick:
        _BASE_BOT_TICK = guarded.core.bot_tick
        guarded.core.bot_tick = _fill_aware_bot_tick


class PositionSyncedHandler(guarded.GuardedHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/bybit/positions":
            payload = annotate_protection(
                collect_open_positions(guarded.core.bybit_request)
            )
            guarded.core.json_response(self, 200, payload)
            return
        return super().do_GET()


def run() -> None:
    guarded.install_guards()
    install_position_management()
    install_mandatory_entry_protection()
    guarded.core.existing_position_guard = _protected_existing_position_guard
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), PositionSyncedHandler)
    print(
        (
            "Guarded Bybit demo backend with verified entry and position "
            f"management running on http://{host}:{port}"
        ),
        flush=True,
    )
    print(f"Reading environment from {guarded.core.ENV_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
