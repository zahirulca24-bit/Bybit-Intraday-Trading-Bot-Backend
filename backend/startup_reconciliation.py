"""Reconcile persistent execution state with Bybit before auto execution starts."""

from __future__ import annotations

import time
from typing import Any

_STATE = {
    "status": "not_run",
    "checkedAt": 0,
    "positions": 0,
    "openOrders": 0,
    "error": None,
}


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("retCode") != 0:
        raise RuntimeError(payload.get("retMsg") or "Bybit reconciliation request failed")
    return list((payload.get("result") or {}).get("list") or [])


def reconcile(core: Any, store: Any) -> dict[str, Any]:
    timestamp = int(time.time())
    store_status = dict(store.status() or {})
    if not store_status.get("ok") or store_status.get("degraded"):
        result = {
            "status": "blocked",
            "checkedAt": timestamp,
            "positions": 0,
            "openOrders": 0,
            "error": "Persistent PostgreSQL state is unavailable.",
        }
        _STATE.update(result)
        with core.BOT_LOCK:
            core.BOT_STATE["enabled"] = False
            core.BOT_STATE["startupReconciliation"] = dict(result)
        return dict(result)

    try:
        positions_payload = core.bybit_request(
            "GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"}
        )
        orders_payload = core.bybit_request(
            "GET", "/v5/order/realtime", {"category": "linear", "settleCoin": "USDT", "openOnly": 0}
        )
        positions = [row for row in _rows(positions_payload) if float(row.get("size") or 0) > 0]
        orders = _rows(orders_payload)
        unresolved_claim = store.get("execution_handoff_active_claim")
        pending_entry = store.get("pending_entry")
        operator_review = bool(
            isinstance(unresolved_claim, dict)
            and unresolved_claim.get("state") in {"CLAIMED", "SUBMITTED", "UNRESOLVED", "SUBMISSION_UNKNOWN"}
        )
        result = {
            "status": "operator_review" if operator_review else "ready",
            "checkedAt": timestamp,
            "positions": len(positions),
            "openOrders": len(orders),
            "unresolvedClaim": unresolved_claim,
            "pendingEntry": pending_entry,
            "error": None,
        }
        store.put(
            "startup_exchange_snapshot",
            {"positions": positions, "openOrders": orders, "checkedAt": timestamp},
        )
        if callable(getattr(store, "record_reconciliation", None)):
            store.record_reconciliation(result["status"], result)
        _STATE.update(result)
        with core.BOT_LOCK:
            core.BOT_STATE["startupReconciliation"] = dict(result)
            if operator_review:
                core.BOT_STATE["enabled"] = False
                core.BOT_STATE["lastReason"] = (
                    "Startup reconciliation found an unresolved execution claim; operator review required."
                )
        return dict(result)
    except Exception as exc:
        result = {
            "status": "error",
            "checkedAt": timestamp,
            "positions": 0,
            "openOrders": 0,
            "error": str(exc),
        }
        try:
            if callable(getattr(store, "record_reconciliation", None)):
                store.record_reconciliation("error", result)
        except Exception:
            pass
        _STATE.update(result)
        with core.BOT_LOCK:
            core.BOT_STATE.update({
                "enabled": False,
                "startupReconciliation": dict(result),
                "lastReason": "Startup reconciliation failed; automatic execution is blocked.",
                "executionGuard": {
                    "ok": False,
                    "reason": "Startup reconciliation failed; automatic execution is blocked.",
                },
            })
        return dict(result)


def snapshot() -> dict[str, Any]:
    return dict(_STATE)
