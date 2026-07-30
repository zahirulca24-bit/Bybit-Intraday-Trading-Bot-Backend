"""Verify exchange-side protection for every open Bybit position."""

from __future__ import annotations

from typing import Any


def _positive(value: Any) -> bool:
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def annotate_protection(payload: dict[str, Any]) -> dict[str, Any]:
    """Annotate each open position with TP/SL/trailing protection state."""
    if payload.get("retCode") != 0:
        return payload

    result = dict(payload.get("result") or {})
    rows = []
    unprotected = []
    for source in result.get("list") or []:
        row = dict(source)
        has_stop_loss = _positive(row.get("stopLoss"))
        has_take_profit = _positive(row.get("takeProfit"))
        has_trailing_stop = _positive(row.get("trailingStop"))
        protected = has_stop_loss and (has_take_profit or has_trailing_stop)
        protection = {
            "protected": protected,
            "hasStopLoss": has_stop_loss,
            "hasTakeProfit": has_take_profit,
            "hasTrailingStop": has_trailing_stop,
            "status": "protected" if protected else "missing",
        }
        row["protection"] = protection
        rows.append(row)
        if not protected:
            unprotected.append(str(row.get("symbol") or "UNKNOWN"))

    result["list"] = rows
    result["protection"] = {
        "ok": not unprotected,
        "status": "protected" if not unprotected else "blocked",
        "protectedCount": len(rows) - len(unprotected),
        "unprotectedCount": len(unprotected),
        "unprotectedSymbols": unprotected,
        "reason": "All open positions have exchange-side stop loss and profit protection."
        if not unprotected
        else f"Missing exchange-side protection: {', '.join(unprotected)}",
    }
    return {**payload, "result": result}


def protection_gate(payload: Any) -> tuple[bool, str]:
    """Return a fail-closed protection decision for a synchronized payload."""
    if not isinstance(payload, dict):
        return False, "Position protection verification failed: invalid response"
    if payload.get("retCode") != 0:
        return False, str(payload.get("retMsg") or "Position protection verification failed")

    result = payload.get("result")
    if not isinstance(result, dict):
        return False, "Position protection verification failed: invalid result payload"
    rows = result.get("list")
    if not isinstance(rows, list):
        return False, "Position protection verification failed: invalid position list"
    if any(not isinstance(row, dict) for row in rows):
        return False, "Position protection verification failed: invalid position row"

    verified = annotate_protection(payload)
    protection = (verified.get("result") or {}).get("protection") or {}
    if not isinstance(protection, dict) or "ok" not in protection:
        return False, "Position protection verification failed: protection state unavailable"
    return bool(protection.get("ok")), str(protection.get("reason") or "Protection unavailable")
